r"""Seed base resume templates (one per category), reverse-engineered from the
up-to-date PDFs in ~/Desktop/Resume and re-expressed with the self-contained
`ebgaramond` LaTeX package (no system-font install needed for Tectonic).

`seed(store)` is idempotent: it inserts a base=1 doc per category only if that
category has no base yet, so it's safe to call on every startup.
"""

from __future__ import annotations

from .store import Store

_PREAMBLE = r"""\documentclass[a4paper]{article}
\usepackage[a4paper, top=1cm, bottom=1cm, left=1.2cm, right=1.2cm]{geometry}
\usepackage[T1]{fontenc}
\usepackage{ebgaramond}
\usepackage[english]{babel}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{titlesec}

\pagestyle{empty}
\setlength{\parindent}{0pt}

\titleformat{\section}{\fontsize{10}{12}\selectfont\bfseries\uppercase}{}{0pt}{}
\titlespacing{\section}{0pt}{6pt}{4pt}

\newcommand{\bodyfont}{\fontsize{9}{10.5}\selectfont}
\setlist[itemize]{leftmargin=*, nosep, topsep=1pt, itemsep=2.5pt}

\begin{document}
\bodyfont

\begin{center}
    {\fontsize{18}{22}\selectfont \textbf{Abhinav Keswani}} \\
    \vspace{2pt}
    abhinavkeswani4@gmail.com | 609-495-4025 | Hoboken, NJ \\
    \href{https://linkedin.com/in/abhinavkeswani}{linkedin.com/in/abhinavkeswani} | \href{https://github.com/abhinavkeswani/commonsense}{github.com/abhinavkeswani/commonsense}
\end{center}

\vspace{-8pt}
"""

_FOOTER = r"""
\end{document}
"""

_QUANT_BODY = r"""\section*{Education}
\vspace{-4pt}
\rule{\textwidth}{1pt}
\textbf{Stevens Institute of Technology} | Hoboken, NJ \hfill Anticipated Graduation: May 2027 \\
\textbf{Bachelor of Science in Quantitative Finance} \\
\textbf{Advanced Coursework:} Stochastic Calculus, ML for Quantitative Finance, Market Microstructure \& Trading, Stochastic Processes, Portfolio Optimization, Financial Derivatives, Statistics. \\
\textbf{Honors:} TopStep 5x Funded Trader, Eagle Scout Award, Edwin A. Stevens Scholar, Wharton Investment Challenge Top-50.

\section*{Professional Experience}
\vspace{-4pt}
\rule{\textwidth}{1pt}

\textbf{Vincere Trading} | New York, NY \hfill March 2026 -- Present \\
\textit{Algorithmic Trading Developer}
\begin{itemize}
    \item Architected \textbf{Hydra}, a \textbf{15,000+ LOC research-to-production trading framework} for a 30-symbol NASDAQ universe spanning 6.3 years of \textbf{Databento Live OPRA.PILLAR} and \textbf{XNAS.ITCH} tick data with 22-month strict-OOS validation.
    \item Designed a deterministic 5-state \textbf{regime classification streaming filter} combining \textbf{Kalman filter innovation z-scores} with ATR realized-volatility quantiles, audited for covariate-shift via \textbf{Kolmogorov-Smirnov tests}.
    \item Engineered a NAV-bounded live execution engine utilizing parallelized \textbf{Databento Live sockets} with per-process file lock isolation, managing 8 concurrent paper portfolios in a \textbf{2x2x2 factorial A/B race}.
    \item Developed an equity-conditional limit-order method (\textbf{LIM\_EQ}) evaluating queue-depletion sensitivities, dropping \textbf{87\% of counter-trend signals} to validate an OOS strategy yielding \textbf{+141.4\% return, 2.44 Sharpe, and 6.50 Calmar}.
\end{itemize}

\vspace{6pt}
\textbf{QSentia} | Remote \hfill October 2025 -- February 2026 \\
\textit{Junior Quant}
\begin{itemize}
    \item Promoted from Intern to \textbf{Junior Quant} for autonomously expanding the firm's tradeable asset universe; engineered \textbf{Polygon.io} pipelines to ingest and segment \textbf{15-minute bar data} for \textbf{reinforcement learning (RL)} strategies.
    \item Developed a framework to calculate \textbf{Pearson correlation matrices on log-returns}, distinguishing stable relationships from market noise across five frequencies to identify \textbf{strengthening vs. spurious trends}.
    \item Architected a \textbf{multi-modal AI analyst (GPT-4o)} that visually interprets batches of generated heatmap images to output structured \textbf{JSON ``correlation fingerprints''} for the firm's backtesting engine.
\end{itemize}

\vspace{6pt}
\textbf{TopStep Trading Firm} | Remote \hfill September 2023 -- Present \\
\textit{Funded Quantitative Trader \& Algorithmic Developer}
\begin{itemize}
    \item Generated \textbf{\$10,000 net profit} with a strictly managed \textbf{4\% Max Drawdown (Max DD)}; executed intraday strategies based on \textbf{institutional liquidity pools}, \textbf{Volume Profile (VPVR/TPO)}, and \textbf{order flow imbalance}.
    \item Conducted intensive \textbf{backtesting via ProjectX API} utilizing \textbf{XGBoost, Neural Networks}, and \textbf{K-Means Clustering} for regime-specific parameter optimization across \textbf{NQ/ES index derivatives}.
    \item Developed custom \textbf{PineScript} algorithms to automate trade signaling and multi-position risk management across 5 funded accounts.
\end{itemize}

\vspace{6pt}
\textbf{Stevens Student Managed Investment Fund} | Hoboken, NJ \hfill January 2024 -- May 2025 \\
\textit{Risk Analyst}
\begin{itemize}
    \item Quantified risk for a multi-sector \textbf{S\&P 500} portfolio using \textbf{VaR, CVaR, Beta, and Treynor} metrics to optimize hedging strategies; implemented a comprehensive risk screening method for the equity analysis subteam.
    \item Developed a \textbf{PDF-scraping algorithm} in Python to migrate 5+ years of historical portfolio data to the Stevens Skyline API, increasing retrieval speed and enabling historical regime sorting.
\end{itemize}

\section*{Projects and Leadership}
\vspace{-4pt}
\rule{\textwidth}{1pt}

\textbf{CommonSense} \hfill January 2026 -- Present \\
\textit{Lead Developer}
\begin{itemize}
    \item Automated data sourcing and insight-generation for Investment Banking via \textbf{SEC EDGAR API}, providing company financial + management analysis in \textbf{$<$10 minutes} using sitemap crawlers and HTML parsers.
    \item Architected a sentiment analysis pipeline using \textbf{localized AI models (Llama/Mistral)} for large-scale data monitoring with \textbf{100\% data residency} and zero external API dependency.
\end{itemize}

\vspace{6pt}
\textbf{Kappa Sigma Rho Omega} | Hoboken, NJ \hfill November 2024 -- January 2026 \\
\textit{President}
\begin{itemize}
    \item Oversaw actions of all chapter chairmen, facilitated proceedings for \textbf{60+ current members}, and served as primary representative to fraternal and academic organizations.
    \item Organized initiatives to raise over \textbf{\$5,000} and accumulated \textbf{450+ man-hours} of service over the 2024-25 calendar year.
\end{itemize}

\section*{Quantitative Skills}
\vspace{-4pt}
\rule{\textwidth}{1pt}
\textbf{Quant/Math:} Stochastic Calculus, Time-Series Analysis, Pearson Correlation, Kalman Filters, Black-Scholes, Risk Metrics. \\
\textbf{Programming:} Python (Pandas/NumPy/Scikit-learn), R, Java, SQL, PineScript, C++, C\#. \\
\textbf{Markets:} Futures (NQ/ES), OPRA Options, Databento API, HFT Theory, Market Microstructure.
"""

_SWE_BODY = r"""\section*{Education}
\vspace{-4pt}
\rule{\textwidth}{1pt}
\textbf{Stevens Institute of Technology} | Hoboken, NJ \hfill Anticipated Graduation: May 2027 \\
\textbf{Bachelor of Science in Quantitative Finance} \\
\textbf{Honors:} HackMHS 1st Place, HackPHS Top-3 Finish, FIRST Dean's List Finalist, Edwin A. Stevens Scholar.

\section*{Professional Experience}
\vspace{-4pt}
\rule{\textwidth}{1pt}

\textbf{Vincere Trading} | New York, NY \hfill March 2026 -- Present \\
\textit{Algorithmic Trading Developer}
\begin{itemize}
    \item Architected \textbf{Hydra}, a distributed backend framework for systematic trading, balancing millisecond-level execution speed with a rigorous, repeatable research environment for deploying complex market strategies.
    \item Engineered a \textbf{Head/Arms VPS trading relay architecture} tailored for \textbf{real-time systems}, drastically reducing geographical execution latency and optimizing cloud infrastructure costs to ensure high availability.
    \item Integrated the \textbf{Databento API} to build high-fidelity data pipelines; automated the cleaning, normalization, and feature generation of time-series data to accelerate iterative quantitative research.
    \item Implemented robust software lifecycle practices, including versioned artifacts and automated \textbf{executive-ready reporting}, bridging the gap between complex technical outputs and accessible stakeholder summaries.
\end{itemize}

\vspace{6pt}
\textbf{MoFlo} | Remote \hfill June 2025 -- September 2025 \\
\textit{AI Development Intern}
\begin{itemize}
    \item Engineered dynamic sitemap explorers and \textbf{document knowledge pipelines} using recursive traversal to extract and structure unstructured data from \textbf{500+ global web sources}.
    \item Designed modular \textbf{n8n workflows} incorporating conditional logic and dynamic fallback chains, routing tasks between \textbf{GPT-4, Claude 3, and Gemini} to optimize for speed, accuracy, and compliance.
    \item Integrated self-learning database architectures via \textbf{SQL \& Supabase} to identify overlooked data points, restructure relational tables, and reduce manual data processing overhead by \textbf{70\%}.
\end{itemize}

\vspace{6pt}
\textbf{Phia} | New York, NY \hfill September 2025 -- October 2025 \\
\textit{Automation Engineering Intern}
\begin{itemize}
    \item Optimized relational \textbf{database schemas} and deployed AI-driven automation via internal APIs, effectively \textbf{reducing client KPI reporting time by 33\%} while significantly increasing data clarity.
    \item Built comprehensive data aggregation pipelines and a \textbf{client-facing trending dashboard}, empowering brand partners to visualize customer affinity, competitor analytics, and niche market opportunities.
\end{itemize}

\vspace{6pt}
\textbf{QSentia} | Remote \hfill October 2025 -- February 2026 \\
\textit{Junior Quant}
\begin{itemize}
    \item Promoted from Intern to \textbf{Junior Quant} for autonomously engineering a caching-enabled quantitative pipeline using the \textbf{Polygon.io API} to ingest and validate data for core \textbf{reinforcement learning} models.
    \item Architected a multi-modal AI analyst using \textbf{GPT-4o} to visually interpret generated heatmap images, automating complex trend analysis and significantly accelerating the validation of quantitative signals.
\end{itemize}

\section*{Projects and Leadership}
\vspace{-4pt}
\rule{\textwidth}{1pt}

\textbf{CommonSense} | \textit{Lead Architect} \hfill January 2026 -- Present
\begin{itemize}
    \item Engineered robust data ingestion pipelines via the \textbf{SEC EDGAR API} to programmatically fetch exhaustive historical 10-K/10-Q filings, capturing the complete financial timeline of target equities since their first public disclosure.
    \item Automated the parsing of unstructured corporate filings to calculate critical financial ratios and evaluate fundamental health across decades of continuous time-series data.
    \item Synthesized historical financial data and LLM-driven sentiment categorization into a deterministic evaluation framework, outputting definitive long/short directional signals in \textbf{$<$10 minutes}.
    \item Architected the open-source sentiment analysis engine using \textbf{localized AI models (Llama/Mistral)} to categorize financial communication data while maintaining \textbf{100\% data residency} and privacy.
\end{itemize}

\vspace{6pt}
\textbf{Kappa Sigma Rho Omega} | \textit{President} \hfill December 2024 -- January 2026
\begin{itemize}
    \item Directed operations and organizational strategy for a \textbf{60+ member organization}, directly managing a 6-person executive board to ensure compliance with university regulations and operational excellence.
    \item Spearheaded cross-functional stakeholder relationships with alumni and university administration, successfully coordinating complex logistics for large-scale events that drove \textbf{\$5,000+ in charitable fundraising}.
\end{itemize}

\section*{Technical Skills}
\vspace{-4pt}
\rule{\textwidth}{1pt}
\textbf{Languages:} Python (Pandas/NumPy), Java, SQL (PostgreSQL/Supabase), C++, R, Lua, PineScript, C\#. \\
\textbf{Tools:} Databento, Polygon.io, n8n Cloud, Git, Linux, VPS Infrastructure, RBAC, OpenAI API, LoRAs. \\
\textbf{Specialties:} Pipeline Architecture, Distributed Systems, Head/Arms Relays, Schema Optimization, Automation.
"""

_FINANCE_BODY = r"""\section*{Education}
\vspace{-4pt}
\rule{\textwidth}{1pt}
\textbf{Stevens Institute of Technology} | Hoboken, NJ \hfill Anticipated Graduation: May 2027 \\
\textbf{Bachelor of Science in Quantitative Finance} \\
\textbf{Relevant Coursework:} Financial Statement Analysis, Corporate Finance, Accounting, Financial Modeling (DCF/LBO), Market Microstructure \& Trading, Portfolio Optimization, Financial Derivatives. \\
\textbf{Honors:} Edwin A. Stevens Scholar, Eagle Scout Award, TopStep 5x Funded Trader, Wharton Investment Challenge Top-50.

\section*{Professional Experience}
\vspace{-4pt}
\rule{\textwidth}{1pt}

\textbf{Vincere Trading} | New York, NY \hfill March 2026 -- Present \\
\textit{Algorithmic Trading Developer}
\begin{itemize}
    \item Developed internal frameworks to streamline the \textbf{research-to-decision workflow} for systematic trading, focusing on repeatable analytics and high-velocity execution.
    \item Created automated \textbf{executive-ready reporting} that distills technical research into consistent, readable summaries for Senior Management and Deal Teams.
    \item Engineered distributed infrastructure to optimize execution latency via \textbf{Brain/Muscle VPS relays}, integrating \textbf{Databento} for institutional-grade time-series data analysis.
    \item Implemented robust software practices for a research environment: modular design, versioned artifacts, and documented interfaces to improve \textbf{portfolio maintainability}.
\end{itemize}

\vspace{6pt}
\textbf{QSentia} | Remote \hfill October 2025 -- February 2026 \\
\textit{Junior Quant}
\begin{itemize}
    \item Promoted from Intern to \textbf{Junior Quant} for autonomously expanding the firm's asset universe; engineered \textbf{Polygon.io} pipelines to ingest and segment data for statistical validation.
    \item Developed a framework to calculate \textbf{Pearson correlation matrices} on log-returns to distinguish stable relationships from market noise, supporting \textbf{credit-equivalent risk modeling}.
    \item Architected a multi-modal AI analyst to visually interpret data heatmaps and output structured \textbf{JSON reports} used for firm-wide \textbf{sensitivity analysis}.
\end{itemize}

\vspace{6pt}
\textbf{Stevens Student Managed Investment Fund} | Hoboken, NJ \hfill January 2024 -- July 2025 \\
\textit{Risk Analyst}
\begin{itemize}
    \item Performed in-depth \textbf{company and industry research} for S\&P 500 equities; authored comprehensive \textbf{Credit Memos} and \textbf{Investment Memoranda} for the equity selection committee.
    \item Developed a comprehensive \textbf{risk screening method} to evaluate \textbf{Borrower Solvency} and portfolio stability, quantifying metrics including \textbf{VaR, CVaR, Beta, and Treynor}.
    \item Built a \textbf{PDF-scraping algorithm} in Python to migrate 5+ years of historical data to the Stevens Skyline API, enhancing data-driven decision-making speed by \textbf{400\%}.
\end{itemize}

\vspace{6pt}
\textbf{TopStep Trading Firm} | Remote \hfill September 2023 -- Present \\
\textit{Funded Quantitative Trader \& Developer}
\begin{itemize}
    \item Managed capital with a strictly enforced \textbf{4\% Max Drawdown (Max DD)}; generated \textbf{\$10,000 net profit} utilizing institutional liquidity and \textbf{Volume Profile (VPVR/TPO)} analysis.
    \item Developed algorithmic signaling and risk management frameworks to automate trade execution across multiple derivative asset classes.
\end{itemize}

\section*{Projects and Leadership}
\vspace{-4pt}
\rule{\textwidth}{1pt}

\textbf{CommonSense} \hfill January 2026 -- Present \\
\textit{Lead Developer}
\begin{itemize}
    \item Automated the \textbf{Corporate Banking workflow} via SEC EDGAR API, providing automated \textbf{Financial Spreading} and \textbf{Debt Covenant Analysis} for 10-K/10-Q filings in \textbf{$<$10 minutes}.
    \item Built a specialized module to extract and summarize \textbf{Management Discussion \& Analysis (MD\&A)} and Risk Factors, providing a numeric ``Outlook Score'' for industry research.
    \item Architected a sentiment analysis pipeline using \textbf{localized AI models} such as Llama and Mistral to ensure \textbf{100\% data residency} and security for sensitive financial data.
\end{itemize}

\vspace{6pt}
\textbf{Kappa Sigma Rho Omega} | Hoboken, NJ \hfill November 2024 -- January 2026 \\
\textit{President}
\begin{itemize}
    \item Led an organization of \textbf{60+ members}, overseeing all operational logistics, professional representation, and chapter proceedings.
    \item Managed cross-functional stakeholder relationships and coordinated initiatives raising \textbf{\$5,000+ in fundraising} with 450+ total man-hours of service.
\end{itemize}

\section*{Technical Skills}
\vspace{-4pt}
\rule{\textwidth}{1pt}
\textbf{Finance:} Credit Analysis, Financial Modeling (LBO/DCF), Accounting Principles, Risk Management, Bloomberg Terminal, MS Excel (VBA). \\
\textbf{Technology:} Python (Pandas/NumPy), SQL (PostgreSQL/Supabase), n8n Automation, Java, C++, C\#, Git. \\
\textbf{Interpersonal:} Operations Oversight, Relationship Management, Public Speaking, Team Collaboration.
"""

BASE_TEMPLATES: dict[str, str] = {
    "swe": _PREAMBLE + _SWE_BODY + _FOOTER,
    "quant": _PREAMBLE + _QUANT_BODY + _FOOTER,
    "finance": _PREAMBLE + _FINANCE_BODY + _FOOTER,
}

_BASE_LABELS = {"swe": "Base — Software Engineer", "quant": "Base — Quant Dev", "finance": "Base — Finance"}


def seed(store: Store) -> list[int]:
    """Insert a base=1 resume doc per category that lacks one. Idempotent."""
    created: list[int] = []
    for cat, latex in BASE_TEMPLATES.items():
        if store.base_resume_doc(cat) is None:
            doc = store.add_resume_doc(category=cat, label=_BASE_LABELS[cat], latex=latex, base=True)
            created.append(doc["id"])
    return created
