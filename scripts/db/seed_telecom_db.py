"""Lagos Telecom dataset transformation.

Reads ``data/raw/ibm_telco_churn.csv`` and writes:
  - ``data/transformed/lagos_telecom.csv``  (flat transformed file)
  - ``data/seeds/lagos_telecom.sql``        (DDL + INSERTs for 6 tables)
"""

from __future__ import annotations

import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

SEED = 42
random.seed(SEED)
np.random.seed(SEED)

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_CSV = REPO_ROOT / "data" / "raw" / "ibm_telco_churn.csv"
OUT_CSV = REPO_ROOT / "data" / "transformed" / "lagos_telecom.csv"
OUT_SQL = REPO_ROOT / "data" / "seeds" / "lagos_telecom.sql"

USD_TO_NGN = 1550
TODAY = date.today()


# ---------- Nigerian name pools ----------

YORUBA_MALE = [
    "Adebayo", "Oluwaseun", "Taiwo", "Kehinde", "Babatunde", "Olumide",
    "Adewale", "Segun", "Femi", "Tunde", "Rotimi", "Gbenga", "Wole",
    "Kunle", "Tobi", "Dayo", "Sola", "Bola", "Niyi", "Deji",
]
YORUBA_FEMALE = [
    "Adaeze", "Folake", "Yetunde", "Omowunmi", "Bimpe", "Ronke", "Shade",
    "Toyin", "Kemi", "Funmi", "Titi", "Lola", "Sade", "Nike", "Joke",
    "Ife", "Dupe", "Bisi", "Yemi", "Remi",
]
IGBO_MALE = [
    "Chukwuemeka", "Obinna", "Chijioke", "Emeka", "Ifeanyi", "Chidi",
    "Uche", "Kelechi", "Chukwudi", "Ikenna", "Nnamdi", "Ebuka", "Chisom",
    "Onyeka", "Tochukwu",
]
IGBO_FEMALE = [
    "Chioma", "Adaeze", "Ngozi", "Amaka", "Uju", "Ifeoma", "Obiageli",
    "Chinwe", "Nneka", "Uloma", "Adanna", "Chinyere", "Ogechi", "Oluchi",
    "Chiamaka",
]
HAUSA_MALE = [
    "Abdullahi", "Musa", "Ibrahim", "Suleiman", "Usman", "Abubakar",
    "Yusuf", "Aliyu", "Garba", "Haruna", "Bashir", "Kabiru", "Lawal",
    "Nasiru", "Aminu",
]
HAUSA_FEMALE = [
    "Fatima", "Aisha", "Hauwa", "Zainab", "Maryam", "Ramatu", "Bilkisu",
    "Hadiza", "Ruqayya", "Asabe",
]

YORUBA_SURNAMES = [
    "Adeyemi", "Okonkwo", "Balogun", "Fashola", "Okafor", "Adeleke",
    "Afolabi", "Adeola", "Ogundimu", "Babangida", "Oduya", "Lawal",
    "Eze", "Nwosu", "Obi",
]
IGBO_SURNAMES = [
    "Okonkwo", "Nwosu", "Eze", "Obi", "Chukwu", "Nwachukwu", "Okeke",
    "Okafor", "Onyekachi", "Anyanwu", "Nweze", "Obiora", "Ogbu",
    "Onwudiwe", "Ugwu",
]
HAUSA_SURNAMES = [
    "Musa", "Ibrahim", "Suleiman", "Usman", "Abubakar", "Yusuf",
    "Aliyu", "Garba", "Haruna", "Bashir",
]

FIRST_NAMES = {
    "Yoruba": {"Male": YORUBA_MALE, "Female": YORUBA_FEMALE},
    "Igbo":   {"Male": IGBO_MALE,   "Female": IGBO_FEMALE},
    "Hausa":  {"Male": HAUSA_MALE,  "Female": HAUSA_FEMALE},
}
SURNAMES = {
    "Yoruba": YORUBA_SURNAMES,
    "Igbo":   IGBO_SURNAMES,
    "Hausa":  HAUSA_SURNAMES,
}


# ---------- region / city / mapping tables ----------

REGION_WEIGHTS = {
    "Lagos":  35,
    "Abuja":  20,
    "Kano":   15,
    "Rivers": 12,
    "Ogun":    8,
    "Enugu":  10,
}
REGION_CITIES = {
    "Lagos":  ["Lagos Island", "Victoria Island", "Ikeja", "Surulere",
               "Lekki", "Yaba", "Apapa"],
    "Abuja":  ["Garki", "Wuse", "Maitama", "Gwarinpa", "Asokoro", "Jabi"],
    "Kano":   ["Kano Municipal", "Fagge", "Dala", "Gwale", "Nassarawa"],
    "Rivers": ["Port Harcourt", "Obio-Akpor", "Eleme", "Oyigbo"],
    "Ogun":   ["Abeokuta", "Sagamu", "Ijebu-Ode"],
    "Enugu":  ["Enugu", "Nsukka", "Awgu", "Udi"],
}
REGION_NAME_GROUP = {
    "Lagos":  "Yoruba",
    "Kano":   "Hausa",
    "Rivers": "Igbo",
    "Ogun":   "Yoruba",
    "Enugu":  "Igbo",
}

PAYMENT_MAP = {
    "Electronic check":          "USSD transfer",
    "Mailed check":              "Bank branch payment",
    "Bank transfer (automatic)": "Direct bank debit",
    "Credit card (automatic)":   "Card payment (auto-debit)",
}
CONTRACT_MAP = {
    "Month-to-month": "Prepaid monthly",
    "One year":       "Annual contract",
    "Two year":       "Biennial contract",
}

OUTPUT_COLUMNS = [
    "subscriber_id", "full_name", "gender", "region", "city",
    "tenure_months", "contract_type", "payment_method", "paperless_billing",
    "senior_citizen", "has_partner", "has_dependents", "monthly_charge_ngn",
    "total_charges_ngn", "last_recharge_date", "last_recharge_amount_ngn",
    "recharge_count_30d", "data_usage_mb", "call_minutes", "sms_count",
    "complaint_count", "open_tickets", "last_complaint_date",
    "has_phone_service", "has_multiple_lines", "internet_type",
    "has_online_security", "has_online_backup", "has_device_protection",
    "has_tech_support", "streaming_tv", "streaming_movies", "churned",
    "churn_value", "churn_score", "cltv", "churn_reason",
]


# ---------- helpers ----------

def round_to(value: float, base: int) -> int:
    return int(round(value / base) * base)


def yn_to_bool(value) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return str(value).strip().lower() == "yes"


def senior_to_bool(value) -> bool:
    """Senior Citizen may be 0/1 or Yes/No depending on the dataset variant."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return str(value).strip().lower() in ("yes", "1", "true")


def pick_name_group(region: str) -> str:
    if region == "Abuja":
        return random.choices(
            ["Yoruba", "Igbo", "Hausa"], weights=[40, 30, 30]
        )[0]
    return REGION_NAME_GROUP[region]


def random_full_name(gender: str, region: str) -> str:
    group = pick_name_group(region)
    first = random.choice(FIRST_NAMES[group][gender])
    last = random.choice(SURNAMES[group])
    return f"{first} {last}"


def days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


def random_days_ago(low: int, high: int) -> date:
    return days_ago(random.randint(low, high))


def safe_randint(low: int, high: int) -> int:
    if low > high:
        low, high = high, low
    return random.randint(low, high)


def previous_month_start(today: date) -> date:
    if today.month == 1:
        return date(today.year - 1, 12, 1)
    return date(today.year, today.month - 1, 1)


# ---------- transformation ----------

def transform(df: pd.DataFrame) -> pd.DataFrame:
    n = len(df)
    out: dict = {}

    out["subscriber_id"] = [f"NG-{i + 1:05d}" for i in range(n)]

    regions = random.choices(
        list(REGION_WEIGHTS.keys()),
        weights=list(REGION_WEIGHTS.values()),
        k=n,
    )
    cities = [random.choice(REGION_CITIES[r]) for r in regions]
    out["region"] = regions
    out["city"] = cities

    genders = df["Gender"].astype(str).tolist()
    out["full_name"] = [
        random_full_name(g, r) for g, r in zip(genders, regions)
    ]
    out["gender"] = genders

    tenure = df["Tenure Months"].astype(int).tolist()
    out["tenure_months"] = tenure

    out["contract_type"] = [CONTRACT_MAP[c] for c in df["Contract"]]
    out["payment_method"] = [PAYMENT_MAP[p] for p in df["Payment Method"]]
    out["paperless_billing"] = [yn_to_bool(v) for v in df["Paperless Billing"]]
    out["senior_citizen"] = [senior_to_bool(v) for v in df["Senior Citizen"]]
    out["has_partner"] = [yn_to_bool(v) for v in df["Partner"]]
    out["has_dependents"] = [yn_to_bool(v) for v in df["Dependents"]]

    monthly_usd = df["Monthly Charges"].astype(float).tolist()
    monthly_ngn = [round_to(v * USD_TO_NGN, 50) for v in monthly_usd]
    out["monthly_charge_ngn"] = monthly_ngn

    total_usd = pd.to_numeric(df["Total Charges"], errors="coerce").tolist()
    total_ngn: list[int] = []
    for usd, mn, tn in zip(total_usd, monthly_ngn, tenure):
        if usd is None or (isinstance(usd, float) and math.isnan(usd)):
            total_ngn.append(int(mn * tn))
        else:
            total_ngn.append(round_to(float(usd) * USD_TO_NGN, 100))
    out["total_charges_ngn"] = total_ngn

    churned_bools = [yn_to_bool(v) for v in df["Churn Label"]]
    out["churned"] = churned_bools
    out["churn_value"] = df["Churn Value"].astype(int).tolist()
    out["churn_score"] = df["Churn Score"].astype(int).tolist()
    out["cltv"] = df["CLTV"].astype(int).tolist()
    out["churn_reason"] = [
        None if (v is None or (isinstance(v, float) and math.isnan(v))
                 or str(v).strip() == "")
        else str(v)
        for v in df["Churn Reason"]
    ]

    out["has_phone_service"] = [yn_to_bool(v) for v in df["Phone Service"]]
    out["has_multiple_lines"] = [yn_to_bool(v) for v in df["Multiple Lines"]]
    out["internet_type"] = df["Internet Service"].astype(str).tolist()
    out["has_online_security"] = [yn_to_bool(v) for v in df["Online Security"]]
    out["has_online_backup"] = [yn_to_bool(v) for v in df["Online Backup"]]
    out["has_device_protection"] = [
        yn_to_bool(v) for v in df["Device Protection"]
    ]
    out["has_tech_support"] = [yn_to_bool(v) for v in df["Tech Support"]]
    out["streaming_tv"] = [yn_to_bool(v) for v in df["Streaming TV"]]
    out["streaming_movies"] = [yn_to_bool(v) for v in df["Streaming Movies"]]

    last_recharge_date: list[str] = []
    last_recharge_amount: list[int] = []
    recharge_count_30d: list[int] = []
    data_usage_mb: list[int] = []
    call_minutes: list[int] = []
    sms_count: list[int] = []
    complaint_count: list[int] = []
    open_tickets: list[int] = []
    last_complaint_date: list[str | None] = []

    for i in range(n):
        churned = churned_bools[i]
        contract = out["contract_type"][i]
        m_charge = monthly_ngn[i]
        t_months = tenure[i]
        has_phone = out["has_phone_service"][i]
        internet_type = out["internet_type"][i]
        has_internet = internet_type != "No"
        tech_support = out["has_tech_support"][i]
        streaming = out["streaming_tv"][i] or out["streaming_movies"][i]

        if churned:
            d = random_days_ago(15, 90)
        elif t_months > 12:
            d = random_days_ago(1, 7)
        else:
            d = random_days_ago(3, 21)
        last_recharge_date.append(d.isoformat())

        if contract == "Prepaid monthly":
            high = max(500, int(m_charge / 4))
            amount = round_to(safe_randint(500, high), 100)
        else:
            low = int(m_charge * 0.8)
            high = int(m_charge * 1.2)
            amount = round_to(safe_randint(low, high), 100)
        last_recharge_amount.append(amount)

        if churned:
            rc = safe_randint(0, 2)
        elif m_charge > 15000:
            rc = safe_randint(1, 3)
        else:
            rc = safe_randint(3, 12)
        recharge_count_30d.append(rc)

        if not has_internet:
            du = 0
        elif churned:
            du = safe_randint(0, 500)
        elif streaming:
            du = safe_randint(8000, 50000)
        else:
            du = safe_randint(1000, 15000)
        data_usage_mb.append(du)

        if not has_phone:
            cm = 0
        elif churned:
            cm = safe_randint(0, 100)
        else:
            cm = safe_randint(80, 600)
        call_minutes.append(cm)

        if not has_phone:
            sm = 0
        elif churned:
            sm = safe_randint(0, 20)
        else:
            sm = safe_randint(10, 200)
        sms_count.append(sm)

        if tech_support and not churned:
            cc = safe_randint(0, 1)
        elif not tech_support and not churned:
            cc = safe_randint(0, 2)
        elif tech_support and churned:
            cc = safe_randint(1, 3)
        else:
            cc = safe_randint(2, 6)
        complaint_count.append(cc)

        if cc == 0:
            ot = 0
        elif cc <= 2:
            ot = safe_randint(0, 1)
        else:
            ot = safe_randint(1, 2)
        open_tickets.append(ot)

        if cc == 0:
            last_complaint_date.append(None)
        elif churned:
            last_complaint_date.append(random_days_ago(7, 60).isoformat())
        else:
            last_complaint_date.append(random_days_ago(30, 180).isoformat())

    out["last_recharge_date"] = last_recharge_date
    out["last_recharge_amount_ngn"] = last_recharge_amount
    out["recharge_count_30d"] = recharge_count_30d
    out["data_usage_mb"] = data_usage_mb
    out["call_minutes"] = call_minutes
    out["sms_count"] = sms_count
    out["complaint_count"] = complaint_count
    out["open_tickets"] = open_tickets
    out["last_complaint_date"] = last_complaint_date

    return pd.DataFrame(out)[OUTPUT_COLUMNS]


# ---------- validation ----------

def print_summary(df: pd.DataFrame) -> bool:
    n = len(df)
    churned_n = int(df["churned"].sum())
    not_churned_n = n - churned_n

    print("=== Transformation Summary ===")
    print(f"Total rows processed: {n}")
    print(f"Rows with churned = True: {churned_n} ({churned_n / n * 100:.1f}%)")
    print(f"Rows with churned = False: {not_churned_n} "
          f"({not_churned_n / n * 100:.1f}%)")

    print("\n=== Regional Distribution ===")
    region_counts = df["region"].value_counts()
    for region in ["Lagos", "Abuja", "Kano", "Rivers", "Ogun", "Enugu"]:
        c = int(region_counts.get(region, 0))
        print(f"{region}: {c} ({c / n * 100:.1f}%)")

    churned_mask = df["churned"].astype(bool)
    high_risk = int((churned_mask | (df["complaint_count"] >= 3)).sum())
    medium = int(
        (~churned_mask & df["complaint_count"].between(1, 2)).sum()
    )
    low = int((~churned_mask & (df["complaint_count"] == 0)).sum())

    print("\n=== Risk Tier Preview (based on churn label) ===")
    print(f"Expected Critical/High risk: {high_risk}")
    print(f"Expected Medium risk: {medium}")
    print(f"Expected Low risk: {low}")

    null_sub = int(df["subscriber_id"].isna().sum())
    dup_sub = int(df["subscriber_id"].duplicated().sum())
    null_name = int(df["full_name"].isna().sum())
    neg_charge = int((df["monthly_charge_ngn"] < 0).sum())
    null_recharge = int(df["last_recharge_date"].isna().sum())

    print("\n=== Data Quality Checks ===")
    print(f"Null subscriber_ids: {null_sub}")
    print(f"Duplicate subscriber_ids: {dup_sub}")
    print(f"Null full_names: {null_name}")
    print(f"Negative monthly charges: {neg_charge}")
    print(f"Null last_recharge_date: {null_recharge}")

    passed = (null_sub == 0 and dup_sub == 0 and null_name == 0
              and neg_charge == 0 and null_recharge == 0)
    print(f"\nAll checks passed: {'YES' if passed else 'NO'}")
    return passed


# ---------- SQL generation ----------

def sql_str(s) -> str:
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def sql_bool(b) -> str:
    if b is None or (isinstance(b, float) and math.isnan(b)):
        return "FALSE"
    return "TRUE" if bool(b) else "FALSE"


def sql_int(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "NULL"
    return str(int(v))


def sql_num(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "NULL"
    return f"{float(v):.2f}"


def sql_date(v) -> str:
    if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
        return "NULL"
    return f"'{v}'"


DDL = """\
DROP TABLE IF EXISTS churn_labels CASCADE;
DROP TABLE IF EXISTS subscriber_support CASCADE;
DROP TABLE IF EXISTS subscriber_services CASCADE;
DROP TABLE IF EXISTS subscriber_charges CASCADE;
DROP TABLE IF EXISTS subscriber_usage CASCADE;
DROP TABLE IF EXISTS subscribers CASCADE;

CREATE TABLE subscribers (
    subscriber_id       VARCHAR(20) PRIMARY KEY,
    full_name           VARCHAR(255) NOT NULL,
    gender              VARCHAR(10),
    region              VARCHAR(100),
    city                VARCHAR(100),
    tenure_months       INTEGER,
    contract_type       VARCHAR(50),
    payment_method      VARCHAR(100),
    paperless_billing   BOOLEAN DEFAULT FALSE,
    senior_citizen      BOOLEAN DEFAULT FALSE,
    has_partner         BOOLEAN DEFAULT FALSE,
    has_dependents      BOOLEAN DEFAULT FALSE,
    account_created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE subscriber_usage (
    id                  SERIAL PRIMARY KEY,
    subscriber_id       VARCHAR(20) REFERENCES subscribers(subscriber_id),
    month               DATE NOT NULL,
    data_usage_mb       INTEGER DEFAULT 0,
    call_minutes        INTEGER DEFAULT 0,
    sms_count           INTEGER DEFAULT 0,
    recharge_count      INTEGER DEFAULT 0,
    streaming_tv        BOOLEAN DEFAULT FALSE,
    streaming_movies    BOOLEAN DEFAULT FALSE
);

CREATE TABLE subscriber_charges (
    id                          SERIAL PRIMARY KEY,
    subscriber_id               VARCHAR(20) REFERENCES subscribers(subscriber_id),
    monthly_charge_ngn          NUMERIC(12, 2),
    total_charges_ngn           NUMERIC(14, 2),
    last_recharge_date          DATE,
    last_recharge_amount_ngn    NUMERIC(10, 2)
);

CREATE TABLE subscriber_services (
    subscriber_id           VARCHAR(20) PRIMARY KEY REFERENCES subscribers(subscriber_id),
    has_phone_service       BOOLEAN DEFAULT FALSE,
    has_multiple_lines      BOOLEAN DEFAULT FALSE,
    has_internet_service    BOOLEAN DEFAULT FALSE,
    internet_type           VARCHAR(50),
    has_online_security     BOOLEAN DEFAULT FALSE,
    has_online_backup       BOOLEAN DEFAULT FALSE,
    has_device_protection   BOOLEAN DEFAULT FALSE,
    has_tech_support        BOOLEAN DEFAULT FALSE
);

CREATE TABLE subscriber_support (
    subscriber_id           VARCHAR(20) PRIMARY KEY REFERENCES subscribers(subscriber_id),
    complaint_count         INTEGER DEFAULT 0,
    last_complaint_date     DATE,
    open_tickets            INTEGER DEFAULT 0,
    last_ticket_status      VARCHAR(50)
);

CREATE TABLE churn_labels (
    subscriber_id   VARCHAR(20) PRIMARY KEY REFERENCES subscribers(subscriber_id),
    churned         BOOLEAN DEFAULT FALSE,
    churn_value     INTEGER,
    churn_score     INTEGER,
    cltv            INTEGER,
    churn_reason    TEXT
);

CREATE INDEX idx_subscribers_region ON subscribers(region);
CREATE INDEX idx_subscribers_contract ON subscribers(contract_type);
CREATE INDEX idx_subscribers_tenure ON subscribers(tenure_months);
CREATE INDEX idx_usage_subscriber ON subscriber_usage(subscriber_id);
CREATE INDEX idx_usage_month ON subscriber_usage(month);
CREATE INDEX idx_charges_subscriber ON subscriber_charges(subscriber_id);
CREATE INDEX idx_support_complaint_count ON subscriber_support(complaint_count);
CREATE INDEX idx_churn_churned ON churn_labels(churned);
CREATE INDEX idx_churn_score ON churn_labels(churn_score);
"""


def batched(seq: Sequence, n: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def write_inserts(
    f,
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    batch_size: int = 100,
) -> None:
    if not rows:
        return
    cols = ", ".join(columns)
    for batch in batched(rows, batch_size):
        values = ",\n    ".join(f"({', '.join(r)})" for r in batch)
        f.write(f"INSERT INTO {table} ({cols}) VALUES\n    {values};\n")
    f.write("\n")


def generate_sql(df: pd.DataFrame, sql_path: Path) -> None:
    month_value = previous_month_start(TODAY).isoformat()
    records = df.to_dict(orient="records")

    with open(sql_path, "w", encoding="utf-8") as f:
        f.write(
            "-- Lagos Telecom seed data, generated by "
            "scripts/db/seed_telecom_db.py\n"
        )
        f.write(DDL)
        f.write("\n")

        sub_cols = [
            "subscriber_id", "full_name", "gender", "region", "city",
            "tenure_months", "contract_type", "payment_method",
            "paperless_billing", "senior_citizen", "has_partner",
            "has_dependents",
        ]
        sub_rows = [
            [
                sql_str(r["subscriber_id"]),
                sql_str(r["full_name"]),
                sql_str(r["gender"]),
                sql_str(r["region"]),
                sql_str(r["city"]),
                sql_int(r["tenure_months"]),
                sql_str(r["contract_type"]),
                sql_str(r["payment_method"]),
                sql_bool(r["paperless_billing"]),
                sql_bool(r["senior_citizen"]),
                sql_bool(r["has_partner"]),
                sql_bool(r["has_dependents"]),
            ]
            for r in records
        ]
        write_inserts(f, "subscribers", sub_cols, sub_rows)

        usage_cols = [
            "subscriber_id", "month", "data_usage_mb", "call_minutes",
            "sms_count", "recharge_count", "streaming_tv", "streaming_movies",
        ]
        usage_rows = [
            [
                sql_str(r["subscriber_id"]),
                f"'{month_value}'",
                sql_int(r["data_usage_mb"]),
                sql_int(r["call_minutes"]),
                sql_int(r["sms_count"]),
                sql_int(r["recharge_count_30d"]),
                sql_bool(r["streaming_tv"]),
                sql_bool(r["streaming_movies"]),
            ]
            for r in records
        ]
        write_inserts(f, "subscriber_usage", usage_cols, usage_rows)

        charge_cols = [
            "subscriber_id", "monthly_charge_ngn", "total_charges_ngn",
            "last_recharge_date", "last_recharge_amount_ngn",
        ]
        charge_rows = [
            [
                sql_str(r["subscriber_id"]),
                sql_num(r["monthly_charge_ngn"]),
                sql_num(r["total_charges_ngn"]),
                sql_date(r["last_recharge_date"]),
                sql_num(r["last_recharge_amount_ngn"]),
            ]
            for r in records
        ]
        write_inserts(f, "subscriber_charges", charge_cols, charge_rows)

        svc_cols = [
            "subscriber_id", "has_phone_service", "has_multiple_lines",
            "has_internet_service", "internet_type", "has_online_security",
            "has_online_backup", "has_device_protection", "has_tech_support",
        ]
        svc_rows = []
        for r in records:
            internet_type = r["internet_type"]
            has_internet = internet_type != "No"
            svc_rows.append([
                sql_str(r["subscriber_id"]),
                sql_bool(r["has_phone_service"]),
                sql_bool(r["has_multiple_lines"]),
                sql_bool(has_internet),
                sql_str(internet_type),
                sql_bool(r["has_online_security"]),
                sql_bool(r["has_online_backup"]),
                sql_bool(r["has_device_protection"]),
                sql_bool(r["has_tech_support"]),
            ])
        write_inserts(f, "subscriber_services", svc_cols, svc_rows)

        sup_cols = [
            "subscriber_id", "complaint_count", "last_complaint_date",
            "open_tickets", "last_ticket_status",
        ]
        sup_rows = []
        for r in records:
            cc = int(r["complaint_count"])
            ot = int(r["open_tickets"])
            if ot > 0:
                status = "Open"
            elif cc > 0:
                status = "Resolved"
            else:
                status = None
            sup_rows.append([
                sql_str(r["subscriber_id"]),
                sql_int(cc),
                sql_date(r["last_complaint_date"]),
                sql_int(ot),
                sql_str(status),
            ])
        write_inserts(f, "subscriber_support", sup_cols, sup_rows)

        churn_cols = [
            "subscriber_id", "churned", "churn_value", "churn_score",
            "cltv", "churn_reason",
        ]
        churn_rows = [
            [
                sql_str(r["subscriber_id"]),
                sql_bool(r["churned"]),
                sql_int(r["churn_value"]),
                sql_int(r["churn_score"]),
                sql_int(r["cltv"]),
                sql_str(r["churn_reason"]),
            ]
            for r in records
        ]
        write_inserts(f, "churn_labels", churn_cols, churn_rows)


# ---------- main ----------

def main() -> int:
    print(f"Reading raw data from {RAW_CSV}...")
    df_raw = pd.read_csv(RAW_CSV)
    print(f"Loaded {len(df_raw)} rows.\n")

    df = transform(df_raw)
    if not print_summary(df):
        print("\nData quality checks failed; not writing output files.")
        return 1

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    OUT_SQL.parent.mkdir(parents=True, exist_ok=True)

    df.to_csv(OUT_CSV, index=False, encoding="utf-8")
    generate_sql(df, OUT_SQL)

    print(
        f"\nTransformation complete. {len(df)} rows written to "
        f"lagos_telecom.csv. SQL seed file written to lagos_telecom.sql."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
