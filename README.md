<div align="center">

*Atlas AI · Case files built, not chats answered.*

</div><div align="center">

#  Atlas

### *Atlas isn't where research ends. It's where knowledge lives.*

**A Living Research Workspace that transforms research into structured, verifiable, and evolving Living Cases.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)]()
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)]()
[![LangGraph](https://img.shields.io/badge/LangGraph-Agentic_AI-121212?style=for-the-badge)]()
[![ChromaDB](https://img.shields.io/badge/ChromaDB-Vector_DB-5C2D91?style=for-the-badge)]()
[![Groq](https://img.shields.io/badge/Groq-LLM-FF6B35?style=for-the-badge)]()

</div>

---
<img width="1536" height="1024" alt="atlas" src="https://github.com/user-attachments/assets/8006f98b-b830-4db5-a457-6a715a304b06" />

# Overview

Atlas is an AI-powered **Living Research Workspace** designed to help users navigate complex information with confidence.

Unlike traditional AI assistants that generate one-time answers, Atlas organizes every investigation into a **Living Case**—a structured knowledge asset that combines evidence, source verification, confidence analysis, semantic memory, and long-term retrieval.

Every research session becomes traceable, searchable, and reusable.

---

# Why Atlas?

Modern AI tools generate answers.

Atlas generates **knowledge**.

Instead of producing isolated conversations, Atlas builds persistent research cases that preserve:

- Questions
- Sources
- Evidence
- Claims
- Confidence
- Research history
- Semantic memory

Every conclusion remains traceable back to its supporting evidence.

---

# Core Features

## Living Cases

Transform every research query into a structured research case containing:

- Research question
- Executive summary
- Supporting evidence
- Source citations
- Confidence score
- Retrieved documents
- Generated report
- Semantic embeddings

---

## Intelligent Research Pipeline

Atlas performs an end-to-end research workflow:

```mermaid
flowchart TB
    subgraph Client["Frontend — Vanilla JS SPA"]
        UI["index.html<br/>HUD-style single-page app"]
    end

    subgraph API["Backend — FastAPI"]
        Auth["Auth Middleware<br/>JWT-style signed tokens"]
        Routes["REST Routes<br/>research · chat · search · history · analytics · knowledge-base"]
    end

    subgraph Pipeline["LangGraph Multi-Agent Pipeline"]
        direction TB
        N1["1. Initialize Session"] --> N2["2. Search (Tavily)"]
        N2 --> N3["3. Rank Sources<br/>BM25 + cosine + fuzzy + credibility"]
        N3 --> N4["4. Scrape<br/>(async, retry + dedupe)"]
        N4 --> N5["5. Merge Evidence"]
        N5 --> N6["6. Verify Claims<br/>(Groq LLM agent)"]
        N5 --> N7["7. Write Report<br/>(Groq LLM agent)"]
        N6 --> N8["8. Executive Brief<br/>(Groq LLM agent)"]
        N7 --> N8
        N8 --> N9["9. Persist + Index"]
    end

    subgraph External["External Services"]
        Tavily["Tavily<br/>Web Search"]
        Groq["Groq<br/>Llama 3.3 70B"]
    end

    subgraph Storage["Storage"]
        SQLite[("SQLite<br/>reports · sessions · sources<br/>users · analytics")]
        Chroma[("ChromaDB<br/>chunk embeddings<br/>local sentence-transformers")]
    end

    UI -- "Bearer token" --> Auth
    Auth --> Routes
    Routes --> Pipeline
    N2 -.-> Tavily
    N6 -.-> Groq
    N7 -.-> Groq
    N8 -.-> Groq
    N9 --> SQLite
    N9 --> Chroma
    Routes -- "semantic search / chat retrieval" --> Chroma
    Routes -- "history / analytics" --> SQLite
```

---

## Semantic Memory

Research doesn't disappear after generation.

Atlas stores reports inside ChromaDB, enabling:

- Semantic search
- Similar case retrieval
- Knowledge reuse
- Context-aware follow-up questions

---

## Knowledge Navigation

Atlas treats research as a navigable landscape rather than isolated outputs.

Users can:

- Explore previous cases
- Compare findings
- Trace supporting evidence
- Navigate related research
- Continue previous investigations

---

## Source Verification

Every report includes:

- Source attribution
- Evidence mapping
- Confidence scoring
- Citation tracking

This creates transparent and explainable AI-generated research.

---

## Analytics Dashboard

Monitor research activity through:

- Total research cases
- Average confidence
- Knowledge base growth
- Source distribution
- Retrieval statistics

---

# Tech Stack

## Backend

- FastAPI
- Python
- LangGraph
- LangChain
- Groq LLM
- ChromaDB
- SQLite

---

## AI & NLP

- Retrieval-Augmented Generation (RAG)
- Semantic Search
- Embeddings
- Agentic Workflows
- Prompt Engineering

---

## Frontend

- Vanilla JS

## APIs

- Tavily Search API
- Groq API

---

# Project Structure

```
Atlas/

backend/
│
├── api/
├── agents/
├── services/
├── models/
├── database/
├── vector_store/
└── main.py

frontend/
│
├── streamlit_app.py
├── design_system.py
├── utils.py
└── pages/

requirements.txt

README.md
```

---

# Running Locally

## Clone

```bash
git clone https://github.com/shreyajoshi144/atlas.git

cd atlas
```

---

## Create Virtual Environment

```bash
python -m venv .venv
```

Activate

Mac/Linux

```bash
source .venv/bin/activate
```

Windows

```bash
.venv\Scripts\activate
```

---

## Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Configure Environment

Create

```
.env
```

Example

```env
GROQ_API_KEY=YOUR_KEY
TAVILY_API_KEY=YOUR_KEY
```

---

## Start Backend

```bash
cd backend

uvicorn main:app --reload
```

---

## Start Frontend

```bash
cd frontend

streamlit run streamlit_app.py
```

---

# Future Roadmap

- Living Knowledge Graph
- Knowledge Evolution Engine
- Versioned Living Cases
- Perspective-based reports
- Case comparison
- Multi-user collaboration
- PDF & PowerPoint export
- Real-time research monitoring
- Case branching & merging

---



### Atlas

*"Every investigation becomes a Living Case. Every conclusion earns its confidence."*

</div>
