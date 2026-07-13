"""The master CV — a comprehensive profile reverse-engineered from Abhinav's
existing résumés (SWE / Quant / Finance) and up-to-date PDFs.

This is the persistent local context every résumé is generated from. It's seeded
once (tracked by the `profile_seeded` flag) so a fresh install gets the full CV;
after that it's the user's to grow/edit via the Profile chat.
"""

from __future__ import annotations

from . import profile as profile_mod
from .store import Store

_SEEDED_FLAG = "profile_seeded"

SUMMARY = (
    "Quantitative Finance student at Stevens (B.S. '27) and hands-on builder across "
    "quant trading, software/data engineering, and financial research. Architects "
    "local-first AI and data pipelines (Hydra, CommonSense) and systematic trading "
    "systems with a quality-first, anti-hallucination bent."
)

EDUCATION = [
    {
        "school": "Stevens Institute of Technology",
        "degree": "Bachelor of Science in Quantitative Finance (CS Focus)",
        "dates": "Anticipated May 2027",
        "location": "Hoboken, NJ",
        "coursework": (
            "Stochastic Calculus, Machine Learning for Quantitative Finance, Market "
            "Microstructure & Trading, Stochastic Processes, Portfolio Optimization, "
            "Financial Derivatives, Statistics, Financial Statement Analysis, Corporate "
            "Finance, Accounting, Financial Modeling (DCF/LBO), Data Structures"
        ),
        "honors": (
            "TopStep 5x Funded Trader, Eagle Scout Award, Edwin A. Stevens Scholar, "
            "Wharton Investment Challenge Top-50, HackMHS 1st Place, HackPHS Top-3, "
            "FIRST Dean's List Finalist"
        ),
    }
]

ROLES = [
    {
        "company": "Seaport Research Partners", "role": "Software Engineering Intern",
        "dates": "June 2026 -- Present", "location": "New York, NY", "category_hint": "swe",
        "facts": [
            "Automated end-to-end Python and SQL research pipelines on a Linux server to scrape, validate, and process mass market data from Bloomberg (BLP API), FactSet, and internal calculation services",
            "Architected deterministic anti-hallucination NLP frameworks feeding tightly bounded financial context to LLMs, generating institutional-grade earnings write-ups with zero data hallucination",
            "Engineered comparative earnings-revision analysis tools spanning SPX and Russell indices (small to large cap), designing calculation decision trees and interactive Streamlit dashboards",
            "Orchestrated automated compilation of validated data streams into formal client-ready research outputs",
        ],
    },
    {
        "company": "Vincere Trading", "role": "Algorithmic Trading Developer",
        "dates": "March 2026 -- Present", "location": "New York, NY", "category_hint": "quant",
        "facts": [
            "Architected Hydra, a 15,000+ LOC research-to-production trading framework for a 30-symbol NASDAQ universe over 6.3 years of Databento Live OPRA.PILLAR and XNAS.ITCH tick data with 22-month strict-OOS validation",
            "Designed a deterministic 5-state regime-classification streaming filter combining Kalman filter innovation z-scores with ATR realized-volatility quantiles, audited for covariate shift via Kolmogorov-Smirnov tests",
            "Engineered a NAV-bounded live execution engine over parallelized Databento Live sockets with per-process file-lock isolation, running 8 concurrent paper portfolios in a 2x2x2 factorial A/B race",
            "Developed an equity-conditional limit-order method (LIM_EQ) evaluating queue-depletion sensitivities, dropping 87% of counter-trend signals to validate an OOS strategy yielding +141.4% return, 2.44 Sharpe, 6.50 Calmar",
            "Engineered a Head/Arms (Brain/Muscle) VPS trading relay architecture reducing geographical execution latency and cloud cost while ensuring high availability",
        ],
    },
    {
        "company": "QSentia", "role": "Junior Quant",
        "dates": "October 2025 -- February 2026", "location": "Remote", "category_hint": "quant",
        "facts": [
            "Promoted from Intern to Junior Quant for autonomously expanding the firm's tradeable asset universe; engineered Polygon.io pipelines to ingest and segment 15-minute bar data for reinforcement-learning strategies",
            "Built a framework computing Pearson correlation matrices on log-returns across five frequencies to separate stable relationships from market noise (strengthening vs. spurious trends)",
            "Architected a multi-modal AI analyst (GPT-4o) that visually interprets batches of generated heatmap images to output structured JSON 'correlation fingerprints' for the backtesting engine",
        ],
    },
    {
        "company": "TopStep Trading Firm", "role": "Funded Quantitative Trader & Algorithmic Developer",
        "dates": "September 2023 -- Present", "location": "Remote", "category_hint": "quant",
        "facts": [
            "Generated $10,000 net profit under a strict 4% max drawdown; executed intraday strategies on institutional liquidity pools, Volume Profile (VPVR/TPO), and order-flow imbalance",
            "Backtested via ProjectX API using XGBoost, neural networks, and K-Means clustering for regime-specific parameter optimization across NQ/ES index derivatives",
            "Developed custom PineScript algorithms automating trade signaling and multi-position risk management across 5 funded accounts",
        ],
    },
    {
        "company": "MoFlo", "role": "AI Developer & Workflow Automation Engineering Intern",
        "dates": "May 2025 -- September 2025", "location": "Remote", "category_hint": "swe",
        "facts": [
            "Engineered document and knowledge pipelines using LLMs, HTML parsers, and sitemap crawlers for structured data extraction from 500+ global web sources",
            "Designed modular n8n workflows with conditional logic and dynamic fallback chains routing tasks between GPT-4, Claude 3, and Gemini for speed, accuracy, and compliance",
            "Integrated a self-learning database via SQL and Supabase to surface overlooked data points, restructure relational tables, and cut manual data processing overhead by 70%",
        ],
    },
    {
        "company": "Phia", "role": "AI Automation Intern",
        "dates": "September 2025 -- October 2025", "location": "New York, NY", "category_hint": "swe",
        "facts": [
            "Reduced client KPI reporting time by 33% by optimizing relational database schemas and applying AI-driven automation to internal APIs",
            "Built data-aggregation pipelines and a client-facing trending dashboard surfacing customer affinity, competitor analytics, and niche market opportunities",
        ],
    },
    {
        "company": "Stevens Student Managed Investment Fund", "role": "Risk Analyst",
        "dates": "January 2024 -- July 2025", "location": "Hoboken, NJ", "category_hint": "finance",
        "facts": [
            "Quantified risk for a multi-sector S&P 500 portfolio using VaR, CVaR, Beta, and Treynor; implemented a comprehensive risk-screening method for the equity analysis subteam",
            "Performed in-depth company and industry research and authored Credit Memos and Investment Memoranda for the equity selection committee",
            "Built a Python PDF-scraping algorithm migrating 5+ years of historical portfolio data to the Stevens Skyline API, increasing retrieval speed and enabling historical regime sorting",
        ],
    },
    {
        "company": "CommonSense", "role": "Lead Architect",
        "dates": "January 2026 -- Present", "location": "Open-source project", "category_hint": "swe",
        "facts": [
            "Automated Investment Banking / corporate-banking workflows via the SEC EDGAR API, delivering company financial + management analysis (financial spreading, debt-covenant analysis, MD&A/Outlook Score) in <10 minutes using sitemap crawlers and HTML parsers",
            "Architected an open-source sentiment-analysis pipeline using localized AI models (Llama/Mistral) with 100% data residency and zero external API dependency",
            "Synthesized historical financials and LLM-driven sentiment into a deterministic evaluation framework outputting long/short directional signals",
        ],
    },
    {
        "company": "Kappa Sigma Rho Omega", "role": "President",
        "dates": "November 2024 -- January 2026", "location": "Hoboken, NJ", "category_hint": None,
        "facts": [
            "Directed operations for a 60+ member organization and a 6-person executive board, ensuring compliance with university regulations",
            "Coordinated large-scale events driving $5,000+ in charitable fundraising and 450+ man-hours of service over the year",
        ],
    },
]

SKILLS = [
    # Languages
    "Python (Pandas/NumPy/Scikit-learn)", "SQL (PostgreSQL/Supabase)", "Java", "C++", "R",
    "C#", "Lua", "PineScript",
    # Tools & infra
    "Databento", "Polygon.io", "Bloomberg (BLP API)", "FactSet", "Streamlit", "Docker",
    "Linux", "n8n Cloud", "Git", "VPS Infrastructure", "Ollama / localized LLMs", "GPT-4o",
    "RBAC", "OAuth2", "Supabase",
    # Quant / math
    "Stochastic Calculus", "Time-Series Analysis", "Pearson Correlation", "Kalman Filters",
    "Black-Scholes", "VaR / CVaR / Beta / Treynor", "XGBoost", "Neural Networks",
    "K-Means Clustering", "Reinforcement Learning", "Market Microstructure", "HFT Theory",
    # Finance
    "Credit Analysis", "Financial Modeling (LBO/DCF)", "Accounting", "Risk Management",
    "Bloomberg Terminal", "Excel (VBA)",
    # Specialties
    "Pipeline Engineering", "Anti-Hallucination Frameworks", "Schema Optimization",
    "Workflow Automation",
]

CV = {"summary": SUMMARY, "education": EDUCATION, "roles": ROLES, "skills": SKILLS}


def seed(store: Store, *, force: bool = False) -> bool:
    """Load the master CV into the profile. No-op if already seeded (unless force)."""
    if not force and store.get_setting(_SEEDED_FLAG):
        return False
    existing = profile_mod.get(store)
    # Start from the CV, then keep any extra roles/skills the user already added.
    merged = {
        "summary": CV["summary"],
        "education": list(CV["education"]),
        "roles": list(CV["roles"]),
        "skills": list(CV["skills"]),
    }
    seen_roles = {(r["company"].lower(), r["role"].lower()) for r in merged["roles"]}
    for r in existing.get("roles", []):
        if (r.get("company", "").lower(), r.get("role", "").lower()) not in seen_roles:
            merged["roles"].append(r)
    seen_skills = {s.lower() for s in merged["skills"]}
    for s in existing.get("skills", []):
        if s.lower() not in seen_skills:
            merged["skills"].append(s)
    store.set_setting("profile", merged)
    store.set_setting(_SEEDED_FLAG, True)
    return True
