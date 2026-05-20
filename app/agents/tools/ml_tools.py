"""ML model training and scoring tools for the Model Training Agent.

Production-grade scikit-learn wrappers with:
- Datetime column detection and conversion
- Zero-variance feature removal
- One-hot encoding explosion cap
- 5-fold stratified cross-validation for reliable metrics
- Robust positive-class detection
- Output validation and guardrails

The trained model lives in memory for the duration of a single pipeline
run — no persistence. Tools share state via _MODEL_STORE.
"""

import json
import logging
import uuid
from typing import Any

from uuid import UUID

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, train_test_split
from sklearn.preprocessing import LabelEncoder
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tools.base import Tool, ToolParam
from app.agents.tools.client_db import (
    open_client_engine,
    quote_identifier,
    safe_client_connection,
    validate_identifier,
)
from app.infrastructure.database.client_queries import get_schema_mapping
from app.infrastructure.database.models.connection import Connection

logger = logging.getLogger(__name__)

# Module-level store for trained models. Keyed by model_id (str).
_MODEL_STORE: dict[str, dict[str, Any]] = {}

# In-memory store for prepared feature data so tools can share by reference instead of ferrying massive JSON strings through LLM tool call arguments.
_FEATURE_STORE: dict[str, dict[str, Any]] = {}

# Scored entities store — score_entities writes here so the pipeline can read the full entity list without the LLM ferrying it through final JSON output.
_SCORED_STORE: dict[str, list[dict]] = {}

# ─── Guards and thresholds ──────────────────────────────────────────────
MIN_SAMPLES = 30           # Minimum rows for meaningful ML
MIN_SAMPLES_HARD = 10      # Absolute minimum (will warn)
MAX_FEATURES = 200         # Cap total features to prevent noise
MAX_ONEHOT_PER_COL = 50    # Max dummy columns per categorical
NULL_DROP_THRESHOLD = 0.5  # Drop columns with >50% nulls
QUALITY_THRESHOLD = 0.55   # Minimum accuracy to accept model
CV_FOLDS = 5               # Cross-validation folds


def _clean_model_store() -> None:
    """Clear all stored models, features, and scores (call after pipeline run)."""
    _MODEL_STORE.clear()
    _FEATURE_STORE.clear()
    _SCORED_STORE.clear()


def build_ml_tools(db: AsyncSession, org_id: UUID) -> list[Tool]:
    """Build the set of ML tools available to the Model Training Agent."""

    async def prepare_features(
        target_column: str,
        exclude_columns: str = "",
        limit: int | str = 10000,
    ) -> dict[str, Any]:
        """Query entity table and prepare feature matrix for ML training.

        Fetches rows directly from the client DB — the LLM no longer needs
        to ferry data through tool call arguments.
        """
        limit_val = max(1, min(int(limit), 10000))
        mapping = await get_schema_mapping(db, org_id)
        conn_result = await db.execute(
            select(Connection).where(
                Connection.id == mapping.connection_id,
                Connection.org_id == org_id,
                Connection.deleted_at.is_(None),
            )
        )
        mapped_conn = conn_result.scalar_one_or_none()
        if not mapped_conn:
            return {"error": "Mapped connection is missing or has been deleted"}

        from app.services.studio_file_source_service import (
            execute_file_source_query,
            supports_studio_file_queries,
        )

        if supports_studio_file_queries(mapped_conn):
            table = validate_identifier(mapping.entity_table, "entity table")
            result = await execute_file_source_query(
                mapped_conn,
                f'SELECT * FROM "{table}" LIMIT {limit_val}',
                page=1,
                page_size=limit_val,
            )
            records = result.get("rows", [])
        else:
            engine, conn = await open_client_engine(db, org_id, mapping.connection_id)
            try:
                async with safe_client_connection(engine, conn) as client_conn:
                    table = validate_identifier(mapping.entity_table, "entity table")
                    quoted_table = quote_identifier(table, conn.db_type)
                    sql = f"SELECT * FROM {quoted_table} LIMIT :lim"
                    result = await client_conn.execute(
                        text(sql), {"lim": limit_val}
                    )
                    rows = result.all()
                    col_names = list(result.keys())
                    records = [dict(zip(col_names, row)) for row in rows]
            finally:
                await engine.dispose()

        if not records:
            return {"error": "No data rows found in entity table"}

        df = pd.DataFrame(records)

        if target_column not in df.columns:
            return {
                "error": f"Target column '{target_column}' not found in data. "
                         f"Available columns: {list(df.columns)}",
            }

        # Extract entity IDs now — they get dropped from the feature matrix below.
        entity_id_col = mapping.entity_id_col
        entity_ids = df[entity_id_col].astype(str).tolist() if entity_id_col in df.columns else []

        # ── Validate dataset size ──
        if len(df) < MIN_SAMPLES_HARD:
            return {
                "error": f"Insufficient data: {len(df)} rows (need at least {MIN_SAMPLES_HARD}). "
                         f"ML training requires a meaningful sample size.",
            }

        size_warning = None
        if len(df) < MIN_SAMPLES:
            size_warning = (
                f"Small dataset warning: {len(df)} rows is below the recommended "
                f"minimum of {MIN_SAMPLES}. Model metrics may be unreliable."
            )

        # ── Separate target ──
        y_raw = df[target_column].copy()
        df = df.drop(columns=[target_column])

        # Validate target suitability
        n_target_classes = y_raw.nunique(dropna=True)
        if n_target_classes < 2:
            return {"error": f"Target column '{target_column}' has only {n_target_classes} unique value(s) — need at least 2 classes."}
        if n_target_classes > 20:
            return {"error": f"Target column '{target_column}' has {n_target_classes} unique values — too many classes for classification. Expected ≤20."}

        # ── Drop excluded columns (IDs, names, etc.) ──
        if exclude_columns:
            excl = [c.strip() for c in exclude_columns.split(",") if c.strip()]
            excl_present = [c for c in excl if c in df.columns]
            df = df.drop(columns=excl_present)

        # ── Detect and convert datetime columns ──
        datetime_cols_converted = []
        for col in df.columns:
            if df[col].dtype in ("datetime64[ns]", "datetime64[ns, UTC]"):
                # Convert to days since earliest date
                ref = df[col].min()
                df[col] = (df[col] - ref).dt.total_seconds() / 86400.0
                datetime_cols_converted.append(col)
            elif df[col].dtype == "object":
                # Try to parse string dates
                sample = df[col].dropna().head(20)
                if len(sample) > 0:
                    try:
                        parsed = pd.to_datetime(sample, infer_datetime_format=True)
                        if parsed.notna().sum() >= len(sample) * 0.8:
                            full_parsed = pd.to_datetime(df[col], errors="coerce")
                            ref = full_parsed.min()
                            df[col] = (full_parsed - ref).dt.total_seconds() / 86400.0
                            datetime_cols_converted.append(col)
                    except (ValueError, TypeError, OverflowError):
                        pass

        # ── Drop columns with >50% nulls ──
        null_frac = df.isnull().mean()
        high_null_cols = null_frac[null_frac > NULL_DROP_THRESHOLD].index.tolist()
        if high_null_cols:
            logger.info("Dropping %d columns with >%.0f%% nulls: %s",
                        len(high_null_cols), NULL_DROP_THRESHOLD * 100, high_null_cols)
            df = df.drop(columns=high_null_cols)

        # ── Drop zero-variance columns (constants) ──
        zero_var_cols = []
        for col in df.columns:
            if df[col].nunique(dropna=True) <= 1:
                zero_var_cols.append(col)
        if zero_var_cols:
            logger.info("Dropping %d zero-variance columns: %s", len(zero_var_cols), zero_var_cols)
            df = df.drop(columns=zero_var_cols)

        if df.empty or len(df.columns) == 0:
            return {"error": "No feature columns remain after cleaning (all dropped due to nulls, zero variance, or exclusion)."}

        # ── Encode target variable ──
        le = LabelEncoder()
        try:
            y = le.fit_transform(y_raw.astype(str).fillna("missing"))
        except Exception as e:
            return {"error": f"Failed to encode target column: {e}"}

        # ── Separate numeric vs categorical ──
        numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
        categorical_cols = df.select_dtypes(include=["object", "category", "bool"]).columns.tolist()

        # Impute numeric: median
        for col in numeric_cols:
            median_val = df[col].median()
            df[col] = df[col].fillna(median_val if pd.notna(median_val) else 0)

        # ── Encode categoricals with explosion cap ──
        encoded_parts = [df[numeric_cols]]
        high_card_warning = []
        for col in categorical_cols:
            n_unique = df[col].nunique()
            df[col] = df[col].fillna("missing")
            if n_unique <= MAX_ONEHOT_PER_COL:
                dummies = pd.get_dummies(df[col], prefix=col, drop_first=True)
                encoded_parts.append(dummies)
            else:
                # Label-encode high-cardinality to prevent feature explosion
                col_le = LabelEncoder()
                encoded_parts.append(
                    pd.DataFrame({col: col_le.fit_transform(df[col].astype(str))})
                )
                high_card_warning.append(f"{col} ({n_unique} unique → label-encoded)")

        X = pd.concat(encoded_parts, axis=1)

        # ── Drop any remaining NaN/inf ──
        X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

        # ── Detect data leakage: columns too correlated with the target ──
        leaky_cols: list[str] = []
        for col in X.columns:
            col_vals = X[col].values.astype(np.float64)
            # Binary columns: check exact match against target
            if X[col].nunique() <= 2:
                if np.array_equal(col_vals, y) or np.array_equal(1 - col_vals, y):
                    leaky_cols.append(col)
                    continue
            # Numeric columns: flag near-perfect correlation with target (>0.95)
            if np.issubdtype(X[col].dtype, np.number):
                corr = abs(np.corrcoef(col_vals, y)[0, 1]) if col_vals.std() > 0 else 0.0
                if corr > 0.95:
                    leaky_cols.append(col)
        if leaky_cols:
            logger.warning(
                "Dropping %d leaky columns (perfectly correlated with target): %s",
                len(leaky_cols), leaky_cols,
            )
            X = X.drop(columns=leaky_cols)

        # ── Cap total feature count ──
        if X.shape[1] > MAX_FEATURES:
            logger.warning("Feature count %d exceeds cap %d — keeping top %d by variance",
                           X.shape[1], MAX_FEATURES, MAX_FEATURES)
            variances = X.var()
            top_cols = variances.nlargest(MAX_FEATURES).index.tolist()
            X = X[top_cols]

        feature_names = X.columns.tolist()

        # ── Compute target distribution ──
        unique, counts = np.unique(y, return_counts=True)
        target_dist = {
            le.inverse_transform([u])[0]: int(c)
            for u, c in zip(unique, counts)
        }

        # Check for severe class imbalance
        min_class_pct = min(counts) / sum(counts) * 100
        imbalance_warning = None
        if min_class_pct < 5:
            imbalance_warning = (
                f"Severe class imbalance: minority class is only {min_class_pct:.1f}% "
                f"of data. Model will use balanced class weights to compensate."
            )

        data_id = str(uuid.uuid4())
        _FEATURE_STORE[data_id] = {
            "features": X.values.astype(np.float64),
            "target": y.astype(np.int64),
            "feature_names": feature_names,
            "target_classes": list(le.classes_),
            "entity_ids": entity_ids,
        }

        result = {
            "data_id": data_id,
            "feature_names": feature_names,
            "n_samples": len(X),
            "n_features": len(feature_names),
            "target_distribution": target_dist,
            "target_classes": list(le.classes_),
            "dropped_high_null_columns": high_null_cols,
            "dropped_zero_variance_columns": zero_var_cols,
            "datetime_columns_converted": datetime_cols_converted,
            "numeric_columns_used": numeric_cols,
            "categorical_columns_encoded": categorical_cols,
            "high_cardinality_columns": high_card_warning,
            "class_imbalance_warning": imbalance_warning,
            "size_warning": size_warning,
        }

        logger.info(
            "Feature preparation: %d samples, %d features, target dist: %s, "
            "datetime converted: %d, zero-var dropped: %d",
            len(X), len(feature_names), target_dist,
            len(datetime_cols_converted), len(zero_var_cols),
        )
        return result

    async def train_model(
        data_id: str,
        algorithm: str = "random_forest",
    ) -> dict[str, Any]:
        """Train a model with 5-fold CV for reliable metrics + feature importances.

        Reads prepared features from the in-memory feature store — no need
        for the LLM to ferry massive JSON strings through tool calls.
        """
        store = _FEATURE_STORE.get(data_id)
        if store is None:
            return {"error": f"Feature data '{data_id}' not found. Call prepare_features first."}

        X = store["features"]
        y = store["target"]
        f_names = store["feature_names"]
        t_classes = store["target_classes"]

        if len(X) < MIN_SAMPLES_HARD:
            return {"error": f"Insufficient data: {len(X)} samples (need at least {MIN_SAMPLES_HARD})"}

        if X.shape[1] != len(f_names):
            return {"error": f"Feature matrix has {X.shape[1]} columns but {len(f_names)} feature names provided"}

        n_unique_y = len(np.unique(y))
        if n_unique_y < 2:
            return {"error": "Target variable has only 1 class — cannot train a classifier"}

        if algorithm != "random_forest":
            return {"error": f"Unsupported algorithm: {algorithm}. Use 'random_forest'."}

        # ── 5-fold stratified cross-validation for reliable metrics ──
        n_folds = min(CV_FOLDS, min(np.bincount(y)))  # can't have more folds than minority class
        n_folds = max(n_folds, 2)  # at least 2 folds

        cv_accuracy, cv_precision, cv_recall, cv_f1, cv_auc = [], [], [], [], []
        avg = "binary" if n_unique_y == 2 else "weighted"

        # Detect positive class index once — used for threshold-adjusted P/R/F1
        positive_idx = _detect_positive_class(t_classes)
        # Minority class proportion as decision threshold. At default 0.5, a
        # class_weight="balanced" model often still predicts all-negative on
        # severely imbalanced data, producing P/R/F1=0 despite good AUC.
        minority_threshold = float(np.bincount(y).min()) / len(y) if n_unique_y == 2 else 0.5

        try:
            skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
            for train_idx, test_idx in skf.split(X, y):
                fold_model = RandomForestClassifier(
                    n_estimators=100, max_depth=10, random_state=42,
                    n_jobs=-1, class_weight="balanced",
                )
                fold_model.fit(X[train_idx], y[train_idx])
                y_pred = fold_model.predict(X[test_idx])

                cv_accuracy.append(float(accuracy_score(y[test_idx], y_pred)))

                try:
                    y_proba = fold_model.predict_proba(X[test_idx])
                    if n_unique_y == 2:
                        # Use minority-proportion threshold for meaningful P/R/F1
                        y_pred_thresh = (y_proba[:, positive_idx] >= minority_threshold).astype(int)
                        cv_precision.append(float(precision_score(y[test_idx], y_pred_thresh, average=avg, zero_division=0)))
                        cv_recall.append(float(recall_score(y[test_idx], y_pred_thresh, average=avg, zero_division=0)))
                        cv_f1.append(float(f1_score(y[test_idx], y_pred_thresh, average=avg, zero_division=0)))
                        cv_auc.append(float(roc_auc_score(y[test_idx], y_proba[:, positive_idx])))
                    else:
                        cv_precision.append(float(precision_score(y[test_idx], y_pred, average=avg, zero_division=0)))
                        cv_recall.append(float(recall_score(y[test_idx], y_pred, average=avg, zero_division=0)))
                        cv_f1.append(float(f1_score(y[test_idx], y_pred, average=avg, zero_division=0)))
                        cv_auc.append(float(roc_auc_score(y[test_idx], y_proba, multi_class="ovr", average="weighted")))
                except Exception as _cv_exc:
                    logger.warning(
                        "CV fold proba/threshold metrics failed (%s: %s) — "
                        "falling back to predict(). positive_idx=%s minority_threshold=%s n_unique_y=%s",
                        type(_cv_exc).__name__, _cv_exc, positive_idx, minority_threshold, n_unique_y,
                    )
                    cv_precision.append(float(precision_score(y[test_idx], y_pred, average=avg, zero_division=0)))
                    cv_recall.append(float(recall_score(y[test_idx], y_pred, average=avg, zero_division=0)))
                    cv_f1.append(float(f1_score(y[test_idx], y_pred, average=avg, zero_division=0)))
        except Exception as e:
            return {"error": f"Cross-validation failed: {e}"}

        # ── Train final model on full data for scoring ──
        # Use 80/20 holdout for the confusion matrix, then retrain on ALL data for scoring
        test_size = min(0.2, max(0.1, MIN_SAMPLES_HARD / len(X)))
        try:
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_size, random_state=42, stratify=y,
            )
        except ValueError:
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_size, random_state=42,
            )

        # Final model trained on ALL data for maximum scoring quality
        final_model = RandomForestClassifier(
            n_estimators=100, max_depth=10, random_state=42,
            n_jobs=-1, class_weight="balanced",
        )
        final_model.fit(X, y)

        # Holdout confusion matrix for interpretability
        holdout_model = RandomForestClassifier(
            n_estimators=100, max_depth=10, random_state=42,
            n_jobs=-1, class_weight="balanced",
        )
        holdout_model.fit(X_train, y_train)
        y_holdout_pred = holdout_model.predict(X_test)
        cm = confusion_matrix(y_test, y_holdout_pred)

        # ── Feature importances from the full-data model ──
        importances = final_model.feature_importances_
        feat_imp = sorted(
            [{"feature": name, "importance": round(float(imp), 4)}
             for name, imp in zip(f_names, importances)],
            key=lambda x: x["importance"],
            reverse=True,
        )

        # ── Store model ──
        model_id = str(uuid.uuid4())[:8]
        _MODEL_STORE[model_id] = {
            "model": final_model,
            "feature_names": f_names,
            "target_classes": t_classes,
        }

        # ── Aggregate CV metrics ──
        mean_accuracy = round(float(np.mean(cv_accuracy)), 4)
        std_accuracy = round(float(np.std(cv_accuracy)), 4)
        mean_f1 = round(float(np.mean(cv_f1)), 4)
        mean_auc = round(float(np.mean(cv_auc)), 4) if cv_auc else None

        meets_threshold = mean_accuracy >= QUALITY_THRESHOLD

        # Build confusion matrix as readable dict
        cm_labels = [str(t_classes[i]) if i < len(t_classes) else str(i) for i in range(len(cm))]
        cm_dict = {}
        for i, actual in enumerate(cm_labels):
            for j, predicted in enumerate(cm_labels):
                cm_dict[f"actual_{actual}_predicted_{predicted}"] = int(cm[i][j])

        result = {
            "model_id": model_id,
            "algorithm": algorithm,
            "validation_method": f"{n_folds}-fold stratified cross-validation",
            "accuracy": mean_accuracy,
            "accuracy_std": std_accuracy,
            "precision": round(float(np.mean(cv_precision)), 4),
            "recall": round(float(np.mean(cv_recall)), 4),
            "f1": mean_f1,
            "auc_roc": mean_auc,
            "cv_fold_accuracies": [round(a, 4) for a in cv_accuracy],
            "confusion_matrix": cm_dict,
            "total_samples": len(X),
            "feature_importances": feat_imp[:20],
            "meets_quality_threshold": meets_threshold,
            "quality_threshold": QUALITY_THRESHOLD,
            "quality_note": (
                f"Model accuracy {mean_accuracy:.1%} ± {std_accuracy:.1%} (CV) "
                f"{'MEETS' if meets_threshold else 'DOES NOT MEET'} "
                f"the {QUALITY_THRESHOLD:.0%} minimum threshold. "
                f"F1={mean_f1:.1%}, AUC-ROC={'N/A' if mean_auc is None else f'{mean_auc:.1%}'}."
            ),
        }

        logger.info(
            "Model trained: accuracy=%.4f±%.4f (CV), f1=%.4f, auc=%s, threshold_met=%s",
            mean_accuracy, std_accuracy, mean_f1, mean_auc, meets_threshold,
        )
        return result

    async def score_entities(
        data_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        """Score all entities with the trained model.

        Reads features and entity IDs from the in-memory feature store.
        """
        feat_store = _FEATURE_STORE.get(data_id)
        if feat_store is None:
            return {"error": f"Feature data '{data_id}' not found. Call prepare_features first."}

        if model_id not in _MODEL_STORE:
            return {"error": f"Model '{model_id}' not found. Available: {list(_MODEL_STORE.keys())}"}

        model_store = _MODEL_STORE[model_id]
        model = model_store["model"]
        t_classes = model_store["target_classes"]
        expected_features = model_store["feature_names"]

        X = feat_store["features"]
        ids = feat_store["entity_ids"]

        if len(X) != len(ids):
            return {"error": f"Feature rows ({len(X)}) != entity IDs ({len(ids)})"}

        if X.shape[1] != len(expected_features):
            return {
                "error": f"Feature count mismatch: model expects {len(expected_features)} features "
                         f"but input has {X.shape[1]} columns",
            }

        # ── Replace any NaN/inf that crept in ──
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        # ── Predict probabilities ──
        try:
            probas = model.predict_proba(X)
        except Exception as e:
            return {"error": f"Prediction failed: {e}"}

        # ── Determine the "positive" (risk) class index ──
        positive_idx = _detect_positive_class(t_classes)

        # ── Map probabilities to risk scores ──
        scored = []
        for i, eid in enumerate(ids):
            raw_score = float(probas[i][positive_idx])
            # Clamp to [0.0, 1.0] for safety
            risk_score = round(max(0.0, min(1.0, raw_score)), 4)

            if risk_score >= 0.8:
                risk_tier = "critical"
            elif risk_score >= 0.6:
                risk_tier = "high"
            elif risk_score >= 0.4:
                risk_tier = "medium"
            else:
                risk_tier = "low"

            scored.append({
                "entity_id": str(eid),
                "risk_score": risk_score,
                "risk_tier": risk_tier,
            })

        # Sort by risk score descending
        scored.sort(key=lambda e: e["risk_score"], reverse=True)

        tier_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for s in scored:
            tier_counts[s["risk_tier"]] += 1

        # Store full scored entity list in memory — the pipeline reads it
        # directly so the LLM doesn't have to ferry 7000+ entities through
        # its final JSON output.
        _SCORED_STORE[data_id] = scored

        logger.info(
            "Scored %d entities: critical=%d, high=%d, medium=%d, low=%d (positive_class=%s)",
            len(scored), tier_counts["critical"], tier_counts["high"],
            tier_counts["medium"], tier_counts["low"], t_classes[positive_idx],
        )
        return {
            "total_scored": len(scored),
            "tier_counts": tier_counts,
            "positive_class": t_classes[positive_idx] if positive_idx < len(t_classes) else "unknown",
            "score_range": {
                "min": min(s["risk_score"] for s in scored) if scored else 0,
                "max": max(s["risk_score"] for s in scored) if scored else 0,
                "mean": round(sum(s["risk_score"] for s in scored) / len(scored), 4) if scored else 0,
            },
        }

    # ─── Build tool definitions ─────────────────────────────────────────

    return [
        Tool(
            name="prepare_features",
            description=(
                "Prepare a feature matrix from the entity table for ML training. "
                "Queries the client DB directly — you do NOT need to fetch data first. "
                "Cleans data, converts datetimes to numeric, removes zero-variance columns, "
                "encodes categoricals (one-hot for ≤50 unique, label-encode otherwise), "
                "handles missing values, drops high-null columns. "
                "Returns: {features_json, target_json, feature_names, target_classes, feature_count, target_dist, ...} on success, or {error: str} if the target has <2 or >20 classes / no usable features remain."
            ),
            parameters=[
                ToolParam("target_column", "string", "Name of the target/label column to predict", required=True),
                ToolParam(
                    "exclude_columns", "string",
                    "Comma-separated column names to exclude from features (ALWAYS exclude entity ID and name columns — they are identifiers, not features)",
                    required=False,
                ),
                ToolParam(
                    "limit", "integer",
                    "Max rows to fetch from the entity table (default 10000, max 10000)",
                    required=False,
                ),
            ],
            execute=prepare_features,
            timeout_seconds=60,
        ),
        Tool(
            name="train_model",
            description=(
                "Train a Random Forest classifier on prepared feature data with 5-fold stratified cross-validation. "
                "Reads features from the in-memory store — just pass the data_id from prepare_features. "
                "Returns: {model_id, accuracy_mean, accuracy_std, f1, auc_roc, confusion_matrix, feature_importances (top 20), meets_quality_threshold, quality_note} on success, or {error: str} if data is insufficient / single-class / shape mismatch. "
                "If meets_quality_threshold is false (CV accuracy < 55%), set ml_available=false and fall back to rule-based scoring."
            ),
            parameters=[
                ToolParam("data_id", "string", "Data ID returned by prepare_features", required=True),
                ToolParam("algorithm", "string", "Algorithm to use (only 'random_forest' supported)", required=False),
            ],
            execute=train_model,
            timeout_seconds=120,
        ),
        Tool(
            name="score_entities",
            description=(
                "Score ALL entities using a trained ML model. "
                "Reads features and entity IDs from the in-memory store — just pass data_id and model_id. "
                "Returns a summary: {total_scored, tier_counts, positive_class, score_range}. "
                "The full scored entity list is stored automatically — you do NOT need to include it in your final JSON."
            ),
            parameters=[
                ToolParam("data_id", "string", "Data ID returned by prepare_features (same one used for train_model)", required=True),
                ToolParam("model_id", "string", "Model ID returned from train_model", required=True),
            ],
            execute=score_entities,
            timeout_seconds=60,
        ),
    ]


def _detect_positive_class(classes: list) -> int:
    """Robustly detect the 'positive' (risk/churn) class index.

    Handles: Yes/No, Y/N, 1/0, True/False, Churned/Active, etc.
    Falls back to the last class index if no match found.
    """
    # Normalise all classes to lowercase strings
    norm = [str(c).lower().strip() for c in classes]

    # Explicit positive keywords (order matters — check exact matches first)
    positive_keywords = {
        "yes", "y", "1", "1.0", "true",
        "churned", "churn", "positive", "high",
        "at_risk", "at risk", "inactive", "cancelled", "canceled",
        "lost", "departed", "attrited", "left",
    }

    for i, c in enumerate(norm):
        if c in positive_keywords:
            return i

    # If we have exactly 2 classes, check if one is a known NEGATIVE
    # and return the OTHER one
    if len(classes) == 2:
        negative_keywords = {
            "no", "n", "0", "0.0", "false",
            "active", "retained", "staying", "current", "existing",
        }
        for i, c in enumerate(norm):
            if c in negative_keywords:
                return 1 - i  # return the other class

    # Fallback: last class (sklearn convention for binary)
    return len(classes) - 1
