# Ivy — System Architecture & Capabilities

Local admin AI on the iMac · dual-brain (Gemini → DeepSeek) · iMessage-driven.

**Legend:** 🟩 live & verified · 🟥 blocked (external bot wall) · ⬜ planned / stub

```mermaid
flowchart TD
  subgraph T["1 · Triggers &amp; Entry Points"]
    T1["📩 iMessage<br/>chat.db poller"]:::live
    T2["🔌 HTTP API<br/>X-API-Key"]:::live
    T3["⏰ launchd schedules"]:::live
  end

  subgraph G["2 · Gateway — FastAPI v3.0"]
    G1["🔑 Shared-secret auth"]:::live
    G2["🚦 Rate limiting"]:::live
    G3["🧾 Correlation IDs"]:::live
    G4["🛡️ Sanitized errors"]:::live
  end

  subgraph R["3 · Reasoning — dual-brain agent"]
    R1["🧭 MessageProcessor<br/>authorize + trigger"]:::live
    R2["🤖 LangChain AgentExecutor<br/>max 5 steps"]:::live
    R3["🧠 Gemini 2.5 Flash → DeepSeek<br/>failover"]:::live
  end

  subgraph S["4 · Skills / Tools"]
    S1["📅 Calendar read"]:::live
    S2["✅ Reminders read/write"]:::live
    S3["📚 Readwise highlights"]:::live
    S4["💬 iMessage send"]:::live
    S5["🛒 Grocery staging<br/>checkout-blocked · bot-walled"]:::blocked
  end

  subgraph P["5 · Proactive Agents — scheduled"]
    P1["🎯 Sports picks<br/>Odds API + Grok · net-new dedup"]:::live
    P2["🧠 Ivy brain<br/>always-on"]:::live
    P3["🗓️ Automation stubs<br/>meal plan · chores · watchlist"]:::plan
  end

  subgraph X["6 · External Services &amp; Data"]
    X1["🧠 LLMs<br/>Gemini · DeepSeek · Grok"]:::live
    X2["🍎 Apple / macOS<br/>Calendar · Reminders · Messages"]:::live
    X3["🌐 Web APIs<br/>Readwise · Odds API · X · Google"]:::live
    X4["🛒 HEB · Kroger<br/>Akamai bot wall"]:::blocked
  end

  T --> G --> R --> S --> X
  T3 --> P --> X

  classDef live fill:#dcfce7,stroke:#16a34a,color:#0f172a;
  classDef blocked fill:#fee2e2,stroke:#dc2626,color:#0f172a;
  classDef plan fill:#f1f5f9,stroke:#94a3b8,color:#0f172a;
```

Everything green was exercised live. The only 🟥 is grocery staging (code complete; HEB serves a bot block-page and Kroger drops the connection at the TLS/HTTP-2 layer). ⬜ items are empty `proactive_agents/` scaffolds.
