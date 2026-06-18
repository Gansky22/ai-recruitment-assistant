# AI Recruitment Assistant MVP

A simple web app that connects OpenAI ChatGPT, Anthropic Claude, and Google Gemini.

## Features

- Generate recruitment ad copy
- Translate Chinese / English / Malay
- Improve client replies
- Auto-select AI provider or choose manually

## Local setup

1. Install Python 3.11+
2. Open this folder in terminal
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and paste your API keys:

```bash
cp .env.example .env
```

5. Run:

```bash
uvicorn main:app --reload
```

6. Open:

```text
http://127.0.0.1:8000
```

## Railway deployment

1. Upload this project to GitHub
2. Create a Railway project from the GitHub repo
3. Add environment variables:
   - OPENAI_API_KEY
   - ANTHROPIC_API_KEY
   - GEMINI_API_KEY
4. Railway start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```
