# AluxuryWatches WhatsApp Sales Assistant — Solution Overview

This document describes the n8n workflow exported as **`developing flow (5).json`** (internal name: *developing flow*).

---

## What problem this solves

This automation turns **WhatsApp** into a guided **luxury watch sales channel** for **AluxuryWatches**. It:

- Greets customers and remembers where they are in the **sales funnel** (image or Watch ID → quality tier → shipping address → payment → order).
- Uses **AI** to understand messages, answer **FAQ / policy** questions from a **curated knowledge base**, and **identify watches from photos**.
- Keeps **session state** in a database so conversations survive breaks and can **welcome customers back** after inactivity.
- Can **hand off to a human** when the customer asks for a person or when the chat is in “human care.”
- **Creates WooCommerce orders** when checkout details are complete.

---

## Main capabilities

| Area | What it does |
|------|----------------|
| **WhatsApp channel** | Inbound messages via webhooks; outbound text, images, and **interactive quality buttons**. |
| **Session & memory** | Loads/updates `chat_sessions` (and related fields): funnel stage, watch info, quality, address, payment, follow-up flags, human-care window. |
| **Intent & entities** | **Hybrid routing**: fast rules for images, keywords, 1/2/3 choices, plus **OpenAI** JSON intent for nuanced text (`general_question`, `watch_id_provided`, `address`, etc.). |
| **Product linking** | **Watch ID** lookup against `watch_product_map`; **vision** path: Gemini describes the watch and matches to the map for WooCommerce product/variation IDs. |
| **Conversational AI** | **Hebrew** storefront agent with **RAG** over **Qdrant** (`luxury_watches_kb`); factual answers use the KB tool; replies end with a small **[מאגר: …]** trace tag. |
| **Checkout** | Collects address and payment preference, then **POST**s an order to **WooCommerce REST** (`/wc/v3/orders`). |
| **Operations** | Filters **outbound/admin** messages (`fromMe`), supports **human care** timed release and hand-off messaging. |

---

## Scenarios the flow handles

1. **First contact (text)** — Customer writes without an image: AI explains the business and asks for **Watch ID** or a **clear photo** (`waiting_for_image` / general path).
2. **Watch ID in text** — Customer sends a code (e.g. `AW-12345`): **lookup** on `watch_product_map`, then move toward **quality selection** with product context.
3. **Photo of a watch** — Downloads the image URL, **Gemini** extracts brand/model/reference JSON, **Match product & images** maps to WooCommerce; session stores vision specs and stage.
4. **Quality tier** — Customer taps **Quality 1 / 2 / 3** or types equivalent; flow advances to **shipping address**.
5. **Address** — Detects address via intent or heuristics; optional **format validation** with a corrective message if needed.
6. **Payment** — Customer picks credit card, 50/50 split, or full cash (or keywords); advances to **order creation**.
7. **Order** — Builds payload and calls **WooCommerce**; post-order stage allows **general follow-up** via AI.
8. **General questions** — When the funnel does not force a rigid next step, **KnowledgeBase agent** + **Qdrant** answers store/policy/product questions in **Hebrew**.
9. **Human request** — Phrases like “representative” / “human” / Hebrew equivalents route to **HUMAN_CARE**: hand-off message and DB flags (`in_human_care`, time windows).
10. **Returning customer** — If the last message was long enough ago (**production: 24 hours**; **test mode: 5 minutes** in code), greeting switches to **welcome back** in Hebrew.
11. **Ignore self/echo** — Messages where `fromMe` is true are dropped so bot traffic does not loop.

---

## How routing works (high level)

1. **GREENAPI Trigger** → normalize payload (**Extract Message Data**).
2. **Admin-message check** → only inbound customer messages continue.
3. **Postgres**: get or create **session**, append history.
4. **Normalizing Merge + Intent** (rules) + **Intent extractor** (OpenAI) + **fixed or raw** / **Parse intent JSON** merge into one structured session.
5. **Funnel Guard** sets **`nextAction`** from **funnel_stage** + intent + entities + image/text.
6. **Switch** on `nextAction`: e.g. `IMAGE_IDENTIFICATION`, `WATCH_ID_LOOKUP`, `SEND_ADDRESS_MESSAGE`, `SEND_PAYMENT_MESSAGE`, `CREATE_ORDER`, `RUN_AI_GENERAL`, `HUMAN_CARE`.

---

## Integrations & nodes (one example each)

| Integration | Node type (example name in this flow) | Sample use |
|-------------|----------------------------------------|------------|
| **Green API (WhatsApp)** | Green API Trigger — **GREENAPI Trigger** | Fires on `incomingMessageReceived`. |
| **Green API (send)** | Green API — **Send message1** | Sends `{{ $json.output }}` to `chatId` from **Prepare AI context**. |
| **Green API (interactive)** | Green API — **interactive-buttons_quality** | Sends Quality 1/2/3 button template after identification. |
| **Database (Supabase / Postgres)** | Postgres — **Get chat session** | Reads `chat_sessions` by customer. |
| **Database (updates)** | Postgres — **Update rows in a table** | Persists funnel fields after AI or checkout steps. |
| **Branching** | If — **Session exists?** | New session vs continuing conversation. |
| **Branching** | Switch — **Switch** | Routes by `nextAction` from **Funnel Guard**. |
| **Custom logic** | Code — **Extract Message Data** | Normalizes webhook to `phone_number`, `message_text`, button replies, `media_url`, `fromMe`. |
| **Vision** | Google Gemini — **Analyze an image** | Returns strict JSON: brand, model, reference, materials, dial, etc. |
| **LLM — intent** | OpenAI — **Intent extractor from message** | Outputs JSON: `intent`, `entity_watch_id`, `entity_quality`, `entity_payment`, `is_address`. |
| **LLM — chat** | OpenAI Chat Model — **OpenAI Chat Model** | Powers the agent’s replies (subgraph). |
| **RAG agent** | AI Agent — **KnowldegeBase agent** | Hebrew system prompt + tool use + funnel reminders. |
| **Vector DB** | Qdrant Vector Store — **Qdrant Vector Store** | Retrieval on collection `luxury_watches_kb`, `topK` 6. |
| **Embeddings** | Embeddings OpenAI — **Embeddings OpenAI** | Query embeddings for Qdrant search. |
| **HTTP** | HTTP Request — **HTTP Request** | GET `{{ $json.media_url }}` as file for Gemini. |
| **HTTP** | HTTP Request — **HTTP Request2** | POST `woo_order_payload` to WooCommerce `/wc/v3/orders`. |
| **Merge** | Merge — **Merge** | Combines branches before order extraction/send path. |

---

## Notes for stakeholders

- **Credentials** in the JSON export are implementation-specific (e.g. Green API, Postgres/Supabase, WooCommerce basic auth, OpenAI). Treat names as internal; do not share secrets in client documents.
- **Language**: customer-facing AI replies target **Hebrew**; internal routing uses English labels (e.g. `nextAction` values).
- **Tuning**: **Funnel Guard** includes configurable **welcome-back** mode (`test_minutes` vs `production_hours`).

---

## Related file

- Workflow export: [`developing flow (5).json`](./developing%20flow%20(5).json)
