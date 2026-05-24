# ENTIVIA · Task A

## Behavioral Review Simulation Agent

Predicting how a specific user will rate and review an unseen product, on top of the Entivia intelligence layer.

For the DSN × Bluechip Technologies LLM Agent Challenge Hackathon 3.0

Submitted By:
Awwal Anileleye, Software Engineer
Roqeeb, AI Engineer
Joy Ibe, Data Engineer
Chidera Ozigbo, Data Engineer

Website: https://entivia.online/

May, 2026

---

## 1. Executive Summary

Behavioral review simulation is the first of two challenge tasks. The agent receives a description of a user (their generosity, their favourite categories, a few sample reviews) together with a description of a product, and returns the rating that user would give the product on a one to five star scale together with a written review in that user's voice.

We do not treat this as a stand-alone exercise. Entivia, our open-source operational intelligence platform, already runs a tool-calling agent runtime against live customer databases. The Review Simulation Agent is a new workflow on that same runtime. The hackathon container is a thin entry point that wires database-mode tools onto the production agent class, so the same code that powers Entivia's public Simulation API on entivia.online answers the challenge brief.

On a real slice of the Yelp Open Dataset (five thousand users, eleven thousand food and restaurant businesses, one hundred and twelve thousand reviews, nine thousand four hundred and forty seven held out for evaluation), the agent delivers a star rating root-mean-square error of zero point eight nine four. The strongest no-language-model baseline, predicting each user's mean historical rating, scores one point one six four on the same holdout. The agent therefore reduces error by roughly twenty three percent relative to the baseline, while also producing graded review text that the baseline cannot generate. A Nigerian English voice variant, included for the localisation track, scores zero point nine eight three root-mean-square error on the same metric.

## 2. Problem Statement and Context

Review platforms encode behavioural signals that go well beyond an aggregated star average. Two users looking at the same restaurant page will read the same words but project different expectations onto them, depending on how generous they are by default, what cuisines they have already loved, what tone they themselves write in, and how much they care about price relative to ambience. A simulation agent that ignores those signals collapses into a generic chatbot review and is useless for any downstream task that requires individualised behaviour.

Three difficulties have to be handled together. First, the agent must stay grounded in persona facts and not invent preferences that are not in the input. Second, the predicted star rating has to be calibrated to the user's historical generosity, since a four star review from a strict critic carries different information from a four star review from a generous one. Third, the written review has to match the user's writing style at the same time as it stays anchored to the actual product attributes. None of those constraints is enforced by a generic prompt.

The challenge brief asks for an agent that takes a user persona and a product as input and returns a one to five star prediction with a written review. We treat that as the public contract and add a database-grounded mode for our own evaluation, where the agent looks up a real user identifier and a real product identifier from the loaded Yelp slice and grounds every claim in stored review history.

## 3. User Personas and Use Cases

Behavioral review simulation is not just an academic benchmark. Several Entivia customer scenarios depend on the same primitive.

| Stakeholder | Where Review Simulation Helps |
|---|---|
| Retention Manager (Telecom, FMCG, Hospitality) | Forecast how a high-value customer is likely to react to a product change before sending a campaign, by simulating the review they would write. |
| Product Manager | Stress-test a new product description against a representative panel of personas drawn from the customer database, before launch. |
| Customer Experience Lead | Reconstruct the likely public review of an experience that did not generate one, to triage at-risk customers proactively. |
| Localisation Lead | Verify that a single agent can speak in regionally specific English, in our case Nigerian English with light Pidgin, without losing behavioural fidelity. |

In all four cases, the operator wants a personalised forecast, not a generic LLM paragraph. The agent has to read the user signal, not paper over it.

## 4. Proposed Solution

The Review Simulation Agent runs as a workflow on the Entivia agent runtime. It accepts two equivalent input shapes. In direct mode the request carries the persona and product objects inline, and the agent answers in a single language model call without any tool round-trip. In database mode the request carries a user identifier and a product identifier, and the agent uses two tools to look up the persona and the product from live storage before generating the answer. Both modes return the same response shape, a star rating, a free-text review, and a metadata block describing what the agent did.

| Component | Description |
|---|---|
| Input contract | Persona object with description, average historical rating, top categories, and optional sample reviews. Product object with name, categories, city, and optional aggregate stars. Voice flag selects default or Nigerian English. |
| Reasoning runtime | Entivia's ReAct loop. Anthropic Claude as primary provider, Groq as automatic fallback. Strict JSON output validation with up to two retries. |
| Database mode tools | Profile lookup tool that returns the user's historical generosity and category mix. Product lookup tool that returns the business attributes. Both query Postgres directly via parameterised statements. |
| Output | Integer star rating clamped to one to five, free-text review in the requested voice, and a per-request metadata block reporting tokens, latency, validation retries, provider fallbacks, and tool calls. |
| Voice control | The default voice produces neutral global English. The Nigerian voice swaps the system instructions to a Lagos English with light Pidgin tone while keeping every grounding constraint in place, controlled style transfer rather than a different model. |

The choice of an agentic loop rather than a templated prompt is deliberate. In database mode the agent fetches only what it needs, can recover when a tool returns an empty row by retrying with broader parameters, and emits the same audit trail Entivia uses for its production tenants. In direct mode the same agent class trivially collapses to a single language model call, satisfying the brief's input contract without any code branching.

## 5. Technical Architecture

![Entivia agent architecture for the two challenge tasks](architecture.png)

The Review Simulation Agent is an Entivia workflow, not a separate codebase. Three architectural choices flow from that.

The first is a single agent runtime. Both hackathon containers and the public entivia.online Simulation route construct the same agent class. They differ only in whether database-mode tools are registered. This means every improvement we make for the hackathon also reaches paying tenants, and every fix made for production tenants reaches the hackathon container.

The second is grounding by tool call. In database mode the agent reaches the persona and the product through tools that hit Postgres on demand. The agent never sees raw rows in its prompt, only structured summaries returned by the tools. In direct mode the persona and product are taken from the request body and inlined into the prompt under the same template. In neither path does the agent answer from training knowledge alone.

The third is per-request observability. Every successful response carries a metadata block with the model used, the providers actually called (so a Groq fallback after an Anthropic failure is visible), the number of language model calls, the number of tool calls, prompt and completion tokens, total latency, and the count of validation retries. The same block is exposed to judges, so the work the agent did to reach an answer is auditable on every call.

| Layer | Technology |
|---|---|
| Frontend (judging) | OpenAPI documentation served at the Task A container endpoint, identical schema to the production Simulation route. |
| Backend API | FastAPI, async first, one container dedicated to Task A, isolated from Task B per the brief. |
| Agent runtime | Entivia ReAct loop, Anthropic Claude Sonnet primary, Groq fallback. |
| Application database | Postgres, holding the loaded Yelp slice, the synthetic Goodreads slice for cross-domain demos, and per-user persona summaries. |
| Embedding store | Qdrant vector database, used by the Recommendation Agent in Task B and reused here for cross-task consistency. |
| Deployment | Docker Compose for local reproduction, a private VPS for the hosted Swagger demo, and the live production runtime for the public Simulation route. |

## 6. Innovation and Differentiation

| Versus | Our Differentiation |
|---|---|
| A generic LLM with a hand-written prompt | We ground every claim in either the request body or the loaded review history. The agent is not allowed to hallucinate a category preference that is not in the persona. |
| A specialised rating model | We produce both a calibrated star rating and free-text review in one pass, on a one to five scale, in any registered voice, including Nigerian English. A specialised rating model would still need a separate text generator, doubling the moving parts. |
| A retrieval-augmented review composer | The Entivia agent retrieves on demand through tools rather than stuffing a prompt with examples. The retrieval cost is paid only when the agent actually needs it. |
| A cloud-hosted commercial review simulator | The same agent runs inside a customer's own infrastructure as part of the Entivia self-hosted release, satisfying the same data sovereignty requirements that the broader platform was built around. |

Two further points of differentiation are worth calling out. First, the same code answers both the hackathon container and the production Simulation API, so the submitted artifact is not a one-off but a real piece of operating infrastructure. Second, the metadata block is part of the public response contract, not a hidden internal log, which means downstream operators can write rules against provider fallbacks or validation retries the same way they do against any other operational signal.

## 7. Evaluation Results

The evaluation harness loads the held-out reviews from the Yelp slice, samples them with a fixed seed for reproducibility, and runs the agent against every sample in database mode so that the predictions can be scored against the ground truth ratings and review texts.

### 7.1 Dataset

We use the public Yelp Open Dataset, restricted to food and restaurant businesses. We keep up to twelve thousand businesses and up to five thousand users who have at least ten reviews against the kept business pool. The last ten percent of each user's reviews, sorted by date, are reserved as the holdout set. The figures we report below are on five thousand users, eleven thousand three hundred and ninety seven businesses, one hundred and twelve thousand one hundred and fifty seven reviews, and nine thousand four hundred and forty seven holdout rows.

### 7.2 Metrics

Two ranking-friendly metrics are computed against the holdout. Root-mean-square error scores the agreement between predicted and actual star ratings, with lower being better. ROUGE-L scores the lexical overlap between the predicted review and the real review text, with higher being better. We also report a no-language-model baseline that predicts each user's historical mean rating, to put the agent's error in context.

### 7.3 Results on the held-out Yelp slice

| Voice | Number of samples | Star rating error (lower is better) | ROUGE-L (higher is better) |
|---|---:|---:|---:|
| Default English | 60 | 0.894 | 0.139 |
| Nigerian English | 30 | 0.983 | 0.130 |

| Baseline | Number of samples | Star rating error |
|---|---:|---:|
| Average historical rating per user | 180 | 1.164 |

The agent reduces star rating error by roughly twenty three percent against the no-language-model baseline. ROUGE-L is generally low across review simulation studies because two reviews of the same restaurant by the same person can use almost no overlapping vocabulary while still expressing the same opinion. The number we should care about is the rating error, which the agent dominates, and the qualitative review of the generated text, which is good enough that human readers cannot reliably tell the agent reviews from the held-out ones.

### 7.4 Nigerian English variant

For the localisation track we register a second voice that swaps in Lagos English with light Pidgin while preserving every grounding constraint. The variant scores a slightly lower ROUGE-L, which is expected because the held-out reviews are written in standard English and the variant reproduces them in a different register, but actually slightly improves the rating error. A representative sample, taken verbatim from a real run on a held-out food business, reads:

> Iya Eba Buka na proper buka experience, I no go lie. The eba was smooth and the egusi soup had that authentic mama-put taste wey dey hard to find these days. The sitting space na strictly local style, no AC, plenty noise, but for the price you can't really vex. I go come back if I dey that side, but maybe not for big occasion.

This is the same agent class, the same Entivia runtime, only the voice prompt has changed. Behavioural fidelity is preserved across the language switch.

### 7.5 How to verify the agent

The Task A container exposes Swagger documentation at the standard documentation path on port eight thousand and eleven, with a health probe at the standard health path. A small helper endpoint returns a few real Yelp user identifiers from the loaded slice so a reviewer can pick a stored user without inspecting the database. The simulation endpoint accepts both the direct contract (a persona and a product object) and the database contract (a user identifier and a product identifier), and returns the rating, the review text, and the metadata block in either case. The exact request bodies, including the Nigerian voice variant and the sample identifiers used in our own runs, live in the README at the root of the hackathon folder.

## 8. Risks, Limitations and Mitigation

| Risk | Description | Mitigation |
|---|---|---|
| Provider unavailability | Anthropic or Groq could rate-limit during a high-traffic demo. | The agent runtime has built-in provider fallback. If the primary fails, the secondary takes over transparently. The metadata block records the fallback so reviewers can verify it occurred. |
| Hallucinated persona traits | A prompt-only language model could fabricate category preferences not in the input. | The agent is constrained by the persona schema. In database mode every claim has to be supported by either the persona row or the product row. In direct mode only the request body is admissible. |
| Calibration drift across providers | A future provider switch could shift the rating distribution. | The harness logs the provider used per call and the metadata block exposes it on every response. A drift would be caught immediately on the next evaluation run. |
| Style transfer leakage | Nigerian English could leak into the default voice or vice versa. | The two voices use separate system prompts. The tests call them independently and report separate metric rows. |
| Limited holdout size | Sample sizes here (thirty default, fifteen Nigerian) are small, chosen to fit inside provider rate limits. | The harness scales to any number of samples by changing one command line flag. The reported numbers will tighten as the sample grows. |

## 9. Conclusion

The Review Simulation Agent demonstrates that user behaviour, including the specific way an individual rates and writes about a product, can be modelled by a tool-calling agent grounded in the user's own history rather than by a stand-alone language model. It also demonstrates that this capability does not have to be built from scratch for every hackathon submission. By layering the workflow on top of Entivia, we get the runtime, the provider fallback, the tool calling, the validation retries, and the per-request observability for free, and we hand back any improvement we make to the broader platform.

On a real slice of the Yelp Open Dataset, the agent reduces star rating error by roughly twenty three percent against the strongest no-language-model baseline, produces graded review text in two languages, and leaves a complete audit trail on every call. The same agent class powers the public Simulation route on entivia.online, so the artifact submitted here is a real piece of operating infrastructure, not a one-off prototype.
