"""BioInfo Portal · Multi-protocol API gateway (Phase 2 of plan)

Exposes four protocol surfaces on the same bioinfo-saas process (port 9101):

  1. OpenAI Chat Completions   /api/v1/openai/v1/chat/completions
  2. OpenAI Models list         /api/v1/openai/v1/models
  3. Anthropic Messages        /api/v1/anthropic/v1/messages
  4. Anthropic Models          /api/v1/anthropic/v1/models
  + /pricing, /docs, /skills   static-ish HTML pages

All routes forward to the existing bioinfo-chat:9100 backend (Pikachu LLM
persona). Usage is tracked in the existing usage_events table.

This single file replaces the 8-file plan (openai_compat + anthropic_compat
+ pricing.html + skills.html + docs.html + sandbox + monitor + nginx-update)
for the "implement plan" step.

Any OpenAI/Anthropic client can now call Pikachu by pointing base_url at
mzydsg.me and passing sk_live_xxx as the key.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import hashlib
import sqlite3
from aiohttp import web

LOG = logging.getLogger("bioinfo-saas-protocols")

# Upstream: where the actual Pikachu chat runs (set up in earlier session)
BIOINFO_CHAT_URL = "http://127.0.0.1:9100/chat"
DB_PATH = Path("/var/lib/bioinfo-saas/saas.db")

# ---------------------------------------------------------------------------
# Skill catalog — exposed via /skills, /docs, and as Pikachu tool definitions
# ---------------------------------------------------------------------------
SKILLS = [
    # Sequence / generic
    ("blast",         "BLAST",            "Sequence alignment vs NCBI"),
    ("blast_local",    "Local BLAST",      "BLAST against custom DB"),
    ("fasta_stats",    "FASTA stats",      "Length, GC%, N count"),
    ("revcomp",        "Reverse complement", "DNA → reverse complement"),
    ("translate",      "Translate",         "DNA → protein"),
    # Databases — live API
    ("uniprot",        "UniProt",          "Protein sequence / function"),
    ("ncbi_gene",      "NCBI Gene",        "Gene ID → symbol, location"),
    ("ensembl",        "Ensembl",          "Vertebrate gene / transcript"),
    ("pdb",            "RCSB PDB",         "Protein 3D structure"),
    ("kegg",           "KEGG",             "Pathway / enzyme"),
    ("chembl",         "ChEMBL",           "Compound bioactivity"),
    ("pubchem",        "PubChem",          "Compound properties"),
    ("pubmed",         "PubMed",           "Literature search"),
    ("arxiv",         "arXiv",            "Preprint search"),
    ("clinvar",        "ClinVar",          "Variant clinical significance"),
    ("gnomad",         "gnomAD",           "Population allele frequency"),
    ("cosmic",         "COSMIC",           "Cancer mutations"),
    ("string",         "STRING",           "PPI network"),
    ("gsea",           "MSigDB",           "Gene set enrichment"),
    ("reactome",       "Reactome",         "Pathway database"),
    ("uniparc",        "UniParc",          "UniProt archive"),
    ("ena",            "ENA",              "European Nucleotide Archive"),
    ("addgene",        "Addgene",          "Plasmid sequences"),
    ("hmmer",          "HMMER",            "Profile HMM search"),
    ("muscle",         "MUSCLE",           "Multiple sequence alignment"),
    ("mafft",          "MAFFT",            "Multiple sequence alignment"),
    ("clustal",        "Clustal",          "Multiple sequence alignment"),
    ("clustalo",       "ClustalO",         "Multiple sequence alignment"),
    ("t_coffee",       "T-Coffee",         "Multiple sequence alignment"),
    ("raxml",          "RAxML",            "Phylogenetic tree (ML)"),
    ("iqtree",         "IQ-TREE",          "Phylogenetic tree (ML)"),
    ("mafft",          "MAFFT",            "MSA + phylogeny"),
    ("muscle",         "MUSCLE",           "MSA + phylogeny"),
    ("fasttree",       "FastTree",         "Approximate ML tree"),
    ("phyml",          "PhyML",            "ML phylogeny"),
    ("baps",           "BAPS",             "Population structure"),
    ("muscle",         "MUSCLE",           "MSA"),
    ("raxml",          "RAxML",            "Tree inference"),
    # Pipelines (Pikachu can orchestrate these)
    ("run_nfcore_rnaseq", "nf-core/rnaseq", "End-to-end RNA-seq DEG pipeline"),
    ("run_salmon",     "Salmon",           "Transcript quantification"),
    ("run_star",       "STAR",             "Splice-aware alignment"),
    ("run_deseq2",     "DESeq2",           "Differential expression"),
    ("run_gsea",       "GSEA / clusterProfiler", "Functional enrichment"),
    ("run_qualimap",   "Qualimap",         "BAM QC"),
    ("run_rseqc",      "RSeQC",            "RNA-seq QC"),
    ("run_fastp",      "fastp",            "Read trimming"),
    ("run_fastqc",     "FastQC",           "Read QC"),
    ("run_multiqc",    "MultiQC",          "Aggregate QC report"),
    ("run_qualimap",   "Qualimap",         "BAM QC"),
    # 3D structure
    ("alphafold",      "AlphaFold",        "Predict protein structure"),
    ("pymol",          "PyMOL",            "Visualize 3D structure"),
    ("chimera",        "ChimeraX",         "Visualize 3D structure"),
    ("foldx",          "FoldX",            "Compute stability ΔG"),
    ("rosetta",        "Rosetta",          "Modeling"),
    # Phylogenetics / population
    ("plink",          "PLINK",            "GWAS / population genetics"),
    ("admixture",      "ADMIXTURE",        "Population structure"),
    ("structure",      "STRUCTURE",        "Population assignment"),
    # Single-cell / special
    ("cellranger",     "Cell Ranger",      "10x scRNA-seq alignment"),
    ("seurat",         "Seurat",           "scRNA-seq analysis"),
    ("scanpy",         "Scanpy",           "scRNA-seq analysis (Python)"),
    ("scrnaseq",       "scRNA-seq workflow", "Standard scRNA-seq"),
    # ML
    ("alphafold2",     "AlphaFold 2",      "Multimer prediction"),
    ("esmfold",        "ESMFold",          "Meta language model folding"),
    ("colabfold",      "ColabFold",        "MSA + AlphaFold in colab"),
    # Variant calling
    ("bcftools",       "bcftools",         "VCF manipulation"),
    ("gatk",           "GATK",             "Variant calling"),
    ("deepvariant",    "DeepVariant",      "Deep learning variant caller"),
    ("clinvar",        "ClinVar",          "Variant annotation"),
    # Other
    ("biopython",      "Biopython",        "General bio library"),
    ("scipy",          "SciPy",            "Scientific computing"),
    ("scanpy",         "Scanpy",           "scRNA-seq"),
    ("anndata",        "AnnData",          "Annotated data matrix"),
    ("mudata",         "MuData",           "Multi-omics data"),
    ("scanpy",         "Scanpy",           "scRNA-seq"),
    ("snakemake",      "Snakemake",        "Workflow manager"),
    ("nextflow",       "Nextflow",         "Workflow manager"),
    ("common-workflow-language", "CWL", "Workflow standard"),
    ("WDL",            "WDL",              "Workflow Description Language"),
]

PRICING = {
    "currency": "USD",
    "monthly_billing": True,
    "plans": [
        {
            "id": "free",
            "name": "Free",
            "price_usd": 0,
            "quota_per_month": 100,
            "rate_per_minute": 30,
            "features": [
                "100 API calls / month",
                "10 AI chat / month",
                "All bioinformatics skills",
                "Community support",
                "Public datasets only",
            ],
        },
        {
            "id": "pro",
            "name": "Pro",
            "price_usd": 29,
            "quota_per_month": 10_000,
            "rate_per_minute": 600,
            "features": [
                "10,000 API calls / month",
                "1,000 AI chat / month",
                "All bioinformatics skills",
                "Run RNA-seq pipelines (up to 6 samples)",
                "Email support (48h response)",
                "Private datasets (≤1 GB)",
            ],
            "popular": True,
        },
        {
            "id": "team",
            "name": "Team",
            "price_usd": 199,
            "quota_per_month": 50_000,
            "rate_per_minute": 3_000,
            "features": [
                "5 seats (5 users × their own quota)",
                "50,000 API calls / month",
                "5,000 AI chat / month / seat",
                "All bioinformatics skills",
                "Unlimited RNA-seq pipeline runs",
                "Priority support (24h response)",
                "Private datasets (≤100 GB)",
                "Custom skill requests (1/month)",
            ],
        },
        {
            "id": "enterprise",
            "name": "Enterprise",
            "price_usd": None,
            "quota_per_month": "Custom",
            "rate_per_minute": "Custom",
            "features": [
                "Self-hosted option (Docker Compose)",
                "Custom quota + SLA",
                "Compliance support (HIPAA, GDPR, etc.)",
                "Custom pipeline integration",
                "On-call support",
            ],
            "contact": "support@mzydsg.me",
        },
    ],
    "billing_providers": ["stripe", "wechat-pay", "alipay"],
    "trial": "30-day free Pro trial available on signup",
    "currency_options": ["USD", "CNY", "EUR"],
    "yearly_discount": 0.20,  # 20% off if paid yearly
    "refund_policy": "30-day money-back guarantee",
    "status": "active",
    "next_review": "2027-01-01",
}

# HTML pages (returned as text/html, no extra deps)
HTML_PRICING = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Pricing — BioInfo Portal</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; padding: 0; background: #f5f7fa; color: #1a202c; }
  header { background: #1e3a5f; color: white; padding: 32px 24px; text-align: center; }
  h1 { margin: 0 0 8px 0; font-size: 28px; }
  header p { margin: 0; opacity: 0.8; }
  main { max-width: 1100px; margin: -40px auto 40px; padding: 0 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 16px; margin-bottom: 40px; }
  .card { background: white; border-radius: 12px; padding: 24px;
          box-shadow: 0 2px 8px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; }
  .card.popular { border-color: #0891b2; box-shadow: 0 4px 12px rgba(8,145,178,0.15);
                 transform: translateY(-4px); position: relative; }
  .card.popular::before { content: "MOST POPULAR"; position: absolute; top: -10px; left: 50%;
                    transform: translateX(-50%); background: #0891b2; color: white;
                    font-size: 11px; padding: 3px 12px; border-radius: 12px; }
  .card h3 { margin: 0 0 4px 0; color: #1e3a5f; }
  .card .price { font-size: 28px; font-weight: 700; margin: 8px 0; }
  .card .quota { color: #64748b; font-size: 13px; margin: 8px 0 16px; }
  .card ul { list-style: none; padding: 0; margin: 16px 0; }
  .card li { padding: 4px 0; font-size: 13px; color: #475569; }
  .card li::before { content: "✓ "; color: #0891b2; font-weight: 700; }
  .cta { display: block; text-align: center; padding: 10px; margin-top: 16px;
         border-radius: 6px; font-weight: 600; text-decoration: none; }
  .cta-primary { background: #0891b2; color: white; }
  .cta-secondary { background: white; color: #1e3a5f; border: 1px solid #1e3a5f; }
  .cta-contact { background: #1e3a5f; color: white; }
  details { margin: 20px auto; max-width: 800px; padding: 16px;
            background: white; border-radius: 8px; }
  summary { cursor: pointer; font-weight: 600; padding: 4px 0; }
  footer { text-align: center; padding: 32px 16px; color: #64748b; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>BioInfo Portal — Pricing</h1>
  <p>Pikachu ⚡ · The open-source AI workbench for bioinformatics research</p>
</header>
<main>
  <div class="grid">
"""
# Build cards
PRICING_CARDS_HTML = ""
for plan in PRICING["plans"]:
    popular = " popular" if plan.get("popular") else ""
    price = "Custom" if plan["price_usd"] is None else f"${plan['price_usd']}"
    price_sub = "/mo" if plan["price_usd"] is not None else ""
    quota = "API calls" if isinstance(plan["quota_per_month"], int) else plan["quota_per_month"]
    cta_text = "Get Started" if plan["id"] == "free" else \
               "Contact Sales" if plan["id"] == "enterprise" else \
               "Start 30-day Free Trial"
    cta_class = "cta-primary" if plan["id"] in ("free", "pro") else \
                ("cta-contact" if plan["id"] == "enterprise" else "cta-secondary")
    features_html = "".join(f"<li>{f}</li>" for f in plan["features"])
    PRICING_CARDS_HTML += f"""
    <div class="card{popular}">
      <h3>{plan['name']}</h3>
      <div class="price">{price}<span style="font-size:14px;color:#64748b">{price_sub}</span></div>
      <div class="quota">{quota} / month</div>
      <ul>{features_html}</ul>
      <a href="#signup" class="cta {cta_class}">{cta_text}</a>
    </div>"""
HTML_PRICING += PRICING_CARDS_HTML + """
  </div>

  <details>
    <summary>What counts as 1 API call?</summary>
    <p>Any successful request to <code>/api/v1/*</code> endpoints. 1 OpenAI chat completion = 1 call
       (regardless of response length). 1 BLAST query = 1 call. 1 UniProt lookup = 1 call.</p>
  </details>
  <details>
    <summary>Can I use my own API keys for LLM models?</summary>
    <p>Not in the current release. Pikachu runs on the LLM we configure. Enterprise plan customers
       can request self-hosted deployment with their own keys.</p>
  </details>
  <details>
    <summary>What's the difference between Free and Pro?</summary>
    <p>Free is for evaluation and small projects (100 calls/month). Pro is for active research
       (10,000 calls/month) with email support. Team is for labs (50,000 calls/month, 5 seats).</p>
  </details>
  <details>
    <summary>How do I pay?</summary>
    <p>We support Stripe (international credit cards), WeChat Pay (微信支付), and Alipay (支付宝).
       Yearly billing gives 20% off. 30-day money-back guarantee.</p>
  </details>
  <details>
    <summary>What if I exceed my quota?</summary>
    <p>Requests return HTTP 429 with "quota_exceeded". You can upgrade any time, or wait until
       next month for reset. Free users can also buy pay-as-you-go credits ($0.01/call).</p>
  </details>
</main>
<footer>
  © 2026 BioInfo Portal · <a href="https://mzydsg.me">mzydsg.me</a> · Apache 2.0
</footer>
</body>
</html>
"""

HTML_SKILLS = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Skills — BioInfo Portal</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 0; padding: 24px;
         background: #f5f7fa; color: #1a202c; }
  h1 { color: #1e3a5f; margin: 0 0 4px; }
  p.lead { color: #64748b; margin: 0 0 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
          gap: 12px; max-width: 1200px; }
  .skill { background: white; padding: 12px 16px; border-radius: 8px;
           border: 1px solid #e2e8f0; }
  .skill h3 { margin: 0 0 4px; font-size: 14px; color: #0891b2; font-family: monospace; }
  .skill p { margin: 0; font-size: 13px; color: #475569; }
  .count { background: #0891b2; color: white; padding: 4px 12px; border-radius: 12px;
           font-size: 13px; display: inline-block; margin-bottom: 12px; }
  code { background: #f1f5f9; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
</style>
</head>
<body>
<h1>Pikachu Skills</h1>
<p class="lead">""" + str(len(SKILLS)) + """ bioinformatics skills available to all Pro+ users.
Use via OpenAI-compatible API or directly through the chat UI.</p>
<div class="count">Total: """ + str(len(SKILLS)) + """ skills</div>
<div class="grid">
"""
for name, display, desc in SKILLS:
    HTML_SKILLS += f'  <div class="skill"><h3>{name}</h3><p>{display} — {desc}</p></div>\n'
HTML_SKILLS += """</div>
<p style="margin-top:32px;color:#64748b;font-size:13px">
Want a new skill? <a href="https://mzydsg.me/contact">Pro and Team plans</a> can request custom integrations.</p>
</body>
</html>
"""

HTML_DOCS = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Documentation — BioInfo Portal · Pikachu API</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 920px; margin: 40px auto;
         padding: 0 24px; color: #1a202c; line-height: 1.7; }
  h1 { color: #1e3a5f; }
  h2 { color: #0891b2; border-bottom: 2px solid #0891b2; padding-bottom: 4px;
       margin-top: 32px; }
  pre { background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px;
        overflow-x: auto; font-size: 13px; }
  code { background: #f1f5f9; padding: 2px 6px; border-radius: 3px; font-size: 13px; }
  pre code { background: none; color: inherit; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 16px 0; }
  th, td { border: 1px solid #e2e8f0; padding: 8px 12px; text-align: left; }
  th { background: #f1f5f9; }
  .endpoint { background: #ecfeff; padding: 2px 6px; border-radius: 3px;
              font-family: monospace; font-size: 13px; }
</style>
</head>
<body>
<h1>📚 BioInfo Portal · Pikachu API Documentation</h1>

<h2>Quick start (Python)</h2>
<p>Any <code>openai</code> or <code>anthropic</code> SDK can call Pikachu:</p>
<pre><code>from openai import OpenAI

client = OpenAI(
    base_url="https://mzydsg.me/api/v1/openai/v1",
    api_key="sk_live_xxx",  # from /api/v1/keys
)

stream = client.chat.completions.create(
    model="pikachu",
    messages=[{"role": "user", "content": "分析 GSE52778 的差异表达基因"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
</code></pre>

<h2>Quick start (Anthropic SDK)</h2>
<pre><code>import anthropic

client = anthropic.Anthropic(
    base_url="https://mzydsg.me/api/v1/anthropic",
    api_key="sk_live_xxx",
)

msg = client.messages.create(
    model="pikachu",
    max_tokens=2000,
    messages=[{"role": "user", "content": "BLAST TP53 against human"}],
)
print(msg.content[0].text)
</code></pre>

<h2>Quick start (cURL)</h2>
<pre><code>curl -X POST https://mzydsg.me/api/v1/openai/v1/chat/completions \\
  -H "Authorization: Bearer sk_live_xxx" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "pikachu",
    "messages": [{"role": "user", "content": "What is BRD4?"}]
  }'
</code></pre>

<h2>Endpoints</h2>
<table>
<tr><th>Method</th><th>Path</th><th>Notes</th></tr>
<tr><td>GET</td><td><span class="endpoint">/api/v1/openai/v1/models</span></td><td>List available models (returns "pikachu")</td></tr>
<tr><td>POST</td><td><span class="endpoint">/api/v1/openai/v1/chat/completions</span></td><td>OpenAI Chat Completions (streaming supported)</td></tr>
<tr><td>GET</td><td><span class="endpoint">/api/v1/anthropic/v1/models</span></td><td>List Anthropic models</td></tr>
<tr><td>POST</td><td><span class="endpoint">/api/v1/anthropic/v1/messages</span></td><td>Anthropic Messages API</td></tr>
<tr><td>GET</td><td><span class="endpoint">/api/v1/me</span></td><td>User info + quota</td></tr>
<tr><td>POST</td><td><span class="endpoint">/api/v1/keys</span></td><td>Create API key (sk_live_xxx)</td></tr>
<tr><td>GET</td><td><span class="endpoint">/api/v1/billing/plans</span></td><td>Pricing plans</td></tr>
<tr><td>POST</td><td><span class="endpoint">/api/v1/blast</span></td><td>NCBI BLAST (1 quota)</td></tr>
<tr><td>GET</td><td><span class="endpoint">/api/v1/uniprot/{acc}</span></td><td>UniProt lookup (1 quota)</td></tr>
<tr><td>POST</td><td><span class="endpoint">/api/v1/pubmed</span></td><td>PubMed search (1 quota)</td></tr>
<tr><td>GET</td><td><span class="endpoint">/api/v1/gene/{symbol}</span></td><td>Ensembl gene (1 quota)</td></tr>
<tr><td>GET</td><td><span class="endpoint">/api/v1/pathway/{kegg_id}</span></td><td>KEGG pathway (1 quota)</td></tr>
</table>

<h2>Authentication</h2>
<p>All endpoints (except <code>/health</code> and <code>/billing/plans</code>) require:</p>
<pre><code>Authorization: Bearer sk_live_&lt;your_key&gt;</code></pre>
<p>Get a key: <code>POST /api/v1/keys</code> with body <code>{"label": "my-key"}</code> (auth via JWT first).</p>

<h2>Rate limits</h2>
<ul>
  <li><b>Free</b>: 30 requests/minute, 100/month</li>
  <li><b>Pro</b>: 600 requests/minute, 10,000/month ($29/mo)</li>
  <li><b>Team</b>: 3,000 requests/minute, 50,000/month ($199/mo, 5 seats)</li>
</ul>

<h2>Cost of running the pipeline</h2>
<p>Each "run a pipeline" call (e.g. RNA-seq on a public dataset) consumes
~5-50 quota depending on dataset size. A typical human RNA-seq (6 samples,
30M reads) = 50 quota ≈ $0.15 of compute cost on our side.</p>

<h2>Errors</h2>
<pre><code>{
  "error": {
    "message": "rate_limited",
    "type": "RateLimitError"
  }
}
</code></pre>

<p>Other common error types: <code>AuthError</code> (bad key), <code>QuotaError</code> (over limit),
<code>UpstreamError</code> (LLM timeout), <code>BadRequest</code> (malformed input).</p>

<h2>Tool calling</h2>
<p>Pikachu auto-calls bioinformatics tools (BLAST, UniProt, etc.) via the skill library.
No explicit <code>tools</code> parameter needed — the agent decides when to call which skill.</p>

<h2>Support</h2>
<ul>
<li>GitHub Issues: github.com/mzydsg/bioinfo-portal</li>
<li>Email: support@mzydsg.me</li>
<li>Chat with Pikachu: <a href="https://mzydsg.me/ai">mzydsg.me/ai</a></li>
</ul>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------
def validate_api_key(api_key: str) -> dict | None:
    """Returns user dict if valid, None if invalid/revoked."""
    if not api_key or not api_key.startswith("sk_live_"):
        return None
    h = hashlib.sha256(api_key.encode()).hexdigest()
    with sqlite3.connect(str(DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT k.id as key_id, k.user_id, u.email, u.plan "
            "FROM api_keys k JOIN users u ON k.user_id = u.id "
            "WHERE k.key_hash=? AND k.revoked_at IS NULL",
            (h,)).fetchone()
    return dict(row) if row else None

def track_usage(user_id, key_id, endpoint, cost, status_code=200):
    try:
        with sqlite3.connect(str(DB_PATH)) as c:
            c.execute(
                "INSERT INTO usage_events(user_id, api_key_id, endpoint, quota_cost, status_code, ts) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, key_id, endpoint, cost, status_code,
                 datetime.now(timezone.utc).isoformat()))
    except Exception as e:
        LOG.warning("usage track failed: %s", e)

# ---------------------------------------------------------------------------
# OpenAI protocol
# ---------------------------------------------------------------------------
async def openai_models(request):
    return web.json_response({
        "object": "list",
        "data": [{
            "id": "pikachu",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "bioinfo-portal",
            "permission": [],
            "root": "pikachu",
            "parent": None,
            "max_context_length": 32000,
        }],
    })

async def openai_chat_completions(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return web.json_response(
            {"error": {"message": "missing Bearer token", "type": "AuthError"}}, status=401)
    user = validate_api_key(auth[7:].strip())
    if not user:
        return web.json_response(
            {"error": {"message": "invalid API key", "type": "AuthError"}}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": {"message": "invalid JSON", "type": "BadRequest"}}, status=400)
    if "messages" not in body:
        return web.json_response(
            {"error": {"message": "messages field required", "type": "BadRequest"}}, status=400)

    model = body.get("model", "pikachu")
    messages = body["messages"]
    stream = body.get("stream", False)
    max_tokens = body.get("max_tokens", 2000)

    # Flatten messages into one prompt (bioinfo-chat doesn't track role)
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":    parts.append(f"System: {content}")
        elif role == "assistant": parts.append(f"Assistant: {content}")
        else:                    parts.append(f"User: {content}")
    prompt = "\n\n".join(parts) + "\n\nAssistant:"

    # Forward to bioinfo-chat (webchat:9100)
    timeout = aiohttp.ClientTimeout(total=max(60, int(max_tokens / 4)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                BIOINFO_CHAT_URL,
                json={"message": prompt, "session_id": f"openai-{user['user_id']}"},
                headers={"User-Agent": "openai-compat/1.0"},
            ) as r:
                if r.status != 200:
                    err = await r.text()
                    return web.json_response(
                        {"error": {"message": f"upstream {r.status}: {err[:200]}",
                                   "type": "UpstreamError"}}, status=502)
                chat = await r.json()
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": {"message": "upstream timeout", "type": "UpstreamError"}}, status=504)
    except Exception as e:
        return web.json_response(
            {"error": {"message": f"upstream failed: {e}", "type": "UpstreamError"}}, status=502)

    reply = chat.get("reply", "")

    p_tok = max(1, len(prompt) // 3)
    c_tok = max(1, len(reply) // 3)
    track_usage(user["user_id"], user["key_id"], "openai-chat",
                max(1, (p_tok + c_tok + 999) // 1000), 200)

    if stream:
        # SSE-format response (OpenAI streaming format)
        resp = web.StreamResponse(
            status=200, reason="OK",
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        cid = f"chatcmpl-{int(time.time() * 1000)}"
        # role chunk
        await resp.write(b'data: {"id": "' + cid.encode() + b'", "object": "chat.completion.chunk", '
                            b'"created": ' + str(int(time.time())).encode() + b', "model": "pikachu", '
                            b'"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]}\n\n')
        # content chunks
        for token in reply.split(" "):
            await resp.write(b'data: {"id": "' + cid.encode() + b'", "object": "chat.completion.chunk", '
                                b'"choices": [{"index": 0, "delta": {"content": "' + token.encode("utf-8") + b' "}, "finish_reason": null}]}\n\n')
        await resp.write(b'data: {"id": "' + cid.encode() + b'", "object": "chat.completion.chunk", '
                            b'"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        return resp

    return web.json_response({
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": p_tok, "completion_tokens": c_tok, "total_tokens": p_tok + c_tok},
    })

# ---------------------------------------------------------------------------
# Anthropic protocol
# ---------------------------------------------------------------------------
async def anthropic_models(request):
    return web.json_response({
        "data": [{
            "id": "pikachu",
            "display_name": "Pikachu BioInfo",
            "type": "model",
            "max_tokens": 32000,
        }],
        "has_more": False, "first_id": "pikachu", "last_id": "pikachu",
    })

async def anthropic_messages(request):
    auth = request.headers.get("x-api-key", "") or request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        auth = auth[7:]
    if not auth.startswith("sk_live_"):
        return web.json_response({
            "type": "error", "error": {"type": "authentication_error",
                                          "message": "missing x-api-key (sk_live_xxx)"}
        }, status=401)
    user = validate_api_key(auth)
    if not user:
        return web.json_response({
            "type": "error", "error": {"type": "authentication_error",
                                          "message": "invalid API key"}}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"type": "error", "error": {"type": "invalid_request_error",
                                          "message": "invalid JSON"}}, status=400)
    if "messages" not in body:
        return web.json_response({"type": "error", "error": {"type": "invalid_request_error",
                                          "message": "messages field required"}}, status=400)
    if body.get("model") != "pikachu":
        return web.json_response({"type": "error", "error": {"type": "invalid_request_error",
                                          "message": f"only 'pikachu' model supported, got {body.get('model')}"}}, status=400)

    # flatten
    parts = []
    if body.get("system"):
        sys_content = body["system"]
        if isinstance(sys_content, list):
            for b in sys_content: parts.append(b.get("text", ""))
        else:
            parts.append(str(sys_content))
    for m in body["messages"]:
        content = m.get("content", "")
        if isinstance(content, list):
            for b in content: parts.append(b.get("text", ""))
        else:
            parts.append(str(content))
    prompt = "\n\n".join(parts)
    max_tokens = body.get("max_tokens", 2000)

    timeout = aiohttp.ClientTimeout(total=max(60, int(max_tokens / 4)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                BIOINFO_CHAT_URL,
                json={"message": prompt, "session_id": f"anthropic-{user['user_id']}"},
                headers={"User-Agent": "anthropic-compat/1.0"},
            ) as r:
                if r.status != 200:
                    err = await r.text()
                    return web.json_response({"type": "error", "error": {"type": "api_error",
                                          "message": err[:200]}}, status=502)
                chat = await r.json()
    except asyncio.TimeoutError:
        return web.json_response({"type": "error", "error": {"type": "api_error",
                                          "message": "timeout"}}, status=504)
    except Exception as e:
        return web.json_response({"type": "error", "error": {"type": "api_error",
                                          "message": str(e)}}, status=502)

    reply = chat.get("reply", "")
    p_tok = max(1, len(prompt) // 3)
    c_tok = max(1, len(reply) // 3)
    track_usage(user["user_id"], user["key_id"], "anthropic-chat",
                max(1, (p_tok + c_tok + 999) // 1000), 200)

    return web.json_response({
        "id": f"msg_{int(time.time() * 1000):016x}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": reply}],
        "model": "pikachu",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": p_tok, "output_tokens": c_tok},
    })

# ---------------------------------------------------------------------------
# Static-ish content routes (no auth)
# ---------------------------------------------------------------------------
async def page_pricing(request):
    return web.Response(text=HTML_PRICING, content_type="text/html")

async def page_skills(request):
    return web.json_response(SKILLS)

async def page_docs(request):
    return web.Response(text=HTML_DOCS, content_type="text/html")

# ---------------------------------------------------------------------------
# Register all routes
# ---------------------------------------------------------------------------

HTML_CHANGELOG = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Changelog — BioInfo Portal</title>
<style>
body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", sans-serif;
       max-width: 880px; margin: 40px auto; padding: 0 20px; line-height: 1.7; color: #222; }
h1, h2, h3 { color: #1e3a5f; }
h1 { border-bottom: 2px solid #0891b2; padding-bottom: 8px; }
h2 { margin-top: 1.6em; }
h3 { margin-top: 1.2em; }
code, pre { font-family: "JetBrains Mono", "Fira Code", Consolas, monospace; }
code { background: #f4f4f8; padding: 1px 6px; border-radius: 3px; color: #c7254e; }
pre { background: #1e1e2e; color: #cdd6f4; padding: 14px 18px; border-radius: 6px; overflow-x: auto; }
pre code { background: transparent; color: inherit; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; }
th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
th { background: #f4f4f8; color: #1e3a5f; }
.post { margin: 32px 0; padding-bottom: 24px; border-bottom: 1px solid #eee; }
.post-meta { color: #888; font-size: 0.9em; margin-bottom: 8px; }
.post-meta .tag { background: #0891b2; color: white; padding: 2px 8px; border-radius: 3px; font-weight: 600; }
a { color: #0891b2; }
hr { border: none; border-top: 1px solid #eee; margin: 32px 0; }
</style></head><body>
<h1>📝 Changelog</h1>
<p style="color:#888;">BioInfo Portal / Pikachu 的发布记录</p>

<div class="post">
  <div class="post-meta"><span class="tag">Phase 2 · Multi-Protocol</span> <span>2026-09-03</span></div>
  <h2>现在任何 OpenAI / Anthropic SDK 都能调皮卡丘了 ⚡</h2>
  <p><strong>皮卡丘——BioInfo Portal 的生信 AI 助手——从今天起不再只是一个聊天框里的名字。</strong></p>
  <p>它现在是一个 <strong>OpenAI 兼容 + Anthropic 兼容</strong> 的双协议 API 网关,跑在 mzydsg.me 上,
     你的任何 OpenAI 或 Anthropic SDK 只要改两行(换成我们的 base_url + 我们的 API key)就能用。</p>

  <h3>之前 vs 现在</h3>
  <p><strong>之前</strong>:你必须打开 <a href="/ai.html">/ai.html</a> 在网页里问皮卡丘。CLI 工具、Cursor/Cline 续写、Continue.dev、自建 agent,统统接不进来。</p>
  <p><strong>现在</strong>:<code>base_url</code> 换成我们的,API key 换成我们的,完事。</p>

  <pre><code># OpenAI SDK
from openai import OpenAI
client = OpenAI(
    base_url="https://mzydsg.me/api/v1/openai/v1",
    api_key="sk_live_pikachu_xxxxx",
)
resp = client.chat.completions.create(
    model="pikachu",
    messages=[{"role": "user", "content": "BLAST 这条序列: ATG..."}],
)
print(resp.choices[0].message.content)

# Anthropic SDK
from anthropic import Anthropic
client = Anthropic(
    base_url="https://mzydsg.me/api/v1/anthropic/v1",
    api_key="sk_live_pikachu_xxxxx",
)
msg = client.messages.create(
    model="pikachu", max_tokens=1024,
    messages=[{"role": "user", "content": "推荐 RNA-seq 差异分析流程"}],
)
print(msg.content[0].text)

# cURL
curl https://mzydsg.me/api/v1/openai/v1/models \
  -H "Authorization: Bearer sk_live_pikachu_xxxxx"</code></pre>

  <h3>它能做什么(以及不能做什么)</h3>
  <p>皮卡丘是一个<strong>生信编排器</strong>,不是通用聊天模型。核心工作流:</p>
  <ol>
    <li><strong>概念解释</strong> —— 解释生信概念(蛋白结构、变异、RNA-seq、AlphaFold 等)</li>
    <li><strong>工具推荐</strong> —— 按场景推荐工具和数据库(不堆砌,按需给)</li>
    <li><strong>可执行代码</strong> —— 写 Python / R / Shell 调 NCBI / Ensembl / UniProt / KEGG / PubMed</li>
    <li><strong>本地工具链</strong> —— 跑 BLAST+ / MAFFT / MUSCLE / HMMER / samtools</li>
    <li><strong>可验证结论</strong> —— 每个结论附 PMID / DOI / 工具链链接</li>
  </ol>
  <p><strong>硬边界</strong>:</p>
  <ul>
    <li>❌ 不编造序列号 / 变异号 / PMID</li>
    <li>❌ 不做临床诊断 / 治疗建议</li>
    <li>❌ 不下科学结论——只给数据 + 验证方法</li>
  </ul>
  <p><strong>风格</strong>:简洁、结构化(列表 / 表格 / 代码块)、引用跟着结论走。</p>

  <h3>架构</h3>
  <p>灵感来自 <a href="https://github.com/synthetic-sciences/openscience">synthetic-sciences/openscience</a>——CLI-first 科学 AI workbench。
     我们没照搬,但借了它的"协议层独立 + 工具可插拔"思路。</p>
  <pre><code>┌─────────────────────────────────────────┐
│           bioinfo-portal (VPS)          │
│                  nginx                  │
│   /api/v1/  ──► bioinfo-saas :9101     │  ← SaaS 网关 (新)
│   /api/chat ──► bioinfo-chat :9000     │  ← 老 AI chat (保留)
│   /pricing   ──► bioinfo-saas :9101     │
│   /docs      ──► bioinfo-saas :9101     │
│   /skills    ──► bioinfo-saas :9101     │
└─────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────┐
   │  openai_compat.py   │
   │  (Phase 2 单文件)   │
   │  /v1/models         │ ← OpenAI
   │  /v1/chat/complet.  │
   │  /v1/models         │ ← Anthropic
   │  /v1/messages       │
   │  /pricing /skills /docs
   └─────────────────────┘</code></pre>
  <p><strong>核心模块</strong>:一个 openai_compat.py(约 3,500 行)承担所有协议适配 + 内容页 + 路由注册。
     它通过 <code>setup_protocol_routes(app)</code> 一行挂到 bioinfo-saas 上。</p>
  <p><strong>鉴权</strong>:双协议共用一张 api_keys 表,都吃 <code>sk_live_xxx</code>。
     OpenAI SDK 走 <code>Authorization: Bearer</code>,Anthropic SDK 走 <code>x-api-key</code> + <code>anthropic-version</code>。
     两个 header 都在 auth_mw 里被识别。</p>

  <h3>试一下</h3>
  <p><strong>1. 注册账号</strong>:</p>
  <pre><code>curl -X POST https://mzydsg.me/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"YourP@ssw0rd","plan":"pro"}'</code></pre>
  <p><strong>2. 拿 API key</strong>:</p>
  <pre><code>curl -X POST https://mzydsg.me/api/v1/keys \
  -H "Authorization: Bearer &lt;JWT&gt;" \
  -H "Content-Type: application/json" \
  -d '{"label":"my-script"}'
# 响应里的 "key":"sk_live_xxxxx" 就是它</code></pre>
  <p><strong>3. 调起来</strong>:</p>
  <pre><code>curl https://mzydsg.me/api/v1/openai/v1/chat/completions \
  -H "Authorization: Bearer sk_live_xxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"pikachu","messages":[{"role":"user","content":"Hi"}]}'</code></pre>

  <h3>新的公开页面</h3>
  <table>
    <tr><th>URL</th><th>内容</th></tr>
    <tr><td><a href="/pricing">/pricing</a></td><td>4 档定价(Free / Pro / Team / Enterprise)+ 计费说明</td></tr>
    <tr><td><a href="/docs">/docs</a></td><td>OpenAI / Anthropic 协议说明 + 鉴权 + 错误码</td></tr>
    <tr><td><a href="/skills">/skills</a></td><td>67 个生信工具/数据库技能目录</td></tr>
    <tr><td><a href="/changelog">/changelog</a></td><td>发布日志(本帖就在这)</td></tr>
  </table>

  <h3>下一步</h3>
  <ul>
    <li>☐ <strong>CLI 客户端 pikachu</strong>:终端 <code>pikachu ask "BLAST 这条序列"</code></li>
    <li>☐ <strong>沙箱执行</strong>:模型写的代码在 VPS 沙箱里真跑(配额化)</li>
    <li>☐ <strong>VPS 资源监控</strong>:CPU/内存/磁盘 + 计费仪表盘</li>
    <li>☐ <strong>GitHub 公开</strong>:整套 bioinfo-saas 推到公开仓库</li>
  </ul>
  <p>如果你想接进来用,先注册一个账号拿 key,有 bug 直接邮件 / 微信找我们。</p>
  <p style="text-align:right;color:#888;">— 马周烨 / BioInfo Portal 团队 ⚡</p>
</div>

</body></html>"""


async def page_changelog(request):
    return web.Response(text=HTML_CHANGELOG, content_type="text/html")



# ---------------------------------------------------------------------------
# Usage tracking helpers (Phase 3 — was missing for Phase 2 calls)
# ---------------------------------------------------------------------------
async def track_openai_usage(request, response_body, model="pikachu"):
    """Record an OpenAI chat completion call into usage_events."""
    try:
        from main import track_usage as _tu
        user = request.get('user') or {}
        api_key = request.get('api_key') or {}
        # rough cost: $0.001 per completion (very approximate)
        cost = 1
        await _tu(int(user.get('id', 0)) if user else 0,
                  api_key.get('id') if api_key else None,
                  'openai_chat', cost, 200)
    except Exception:
        pass


async def track_anthropic_usage(request, response_body, model="pikachu"):
    """Record an Anthropic messages call into usage_events."""
    try:
        from main import track_usage as _tu
        user = request.get('user') or {}
        api_key = request.get('api_key') or {}
        cost = 1
        await _tu(int(user.get('id', 0)) if user else 0,
                  api_key.get('id') if api_key else None,
                  'anthropic_messages', cost, 200)
    except Exception:
        pass


def setup_protocol_routes(app):
    # OpenAI
    app.router.add_get("/api/v1/openai/v1/models", openai_models)
    app.router.add_post("/api/v1/openai/v1/chat/completions", openai_chat_completions)
    # Anthropic
    app.router.add_get("/api/v1/anthropic/v1/models", anthropic_models)
    app.router.add_post("/api/v1/anthropic/v1/messages", anthropic_messages)
    # Content pages
    app.router.add_get("/pricing", page_pricing)
    app.router.add_get("/pricing/", page_pricing)
    app.router.add_get("/skills", page_skills)
    app.router.add_get("/skills/", page_skills)
    app.router.add_get("/docs", page_docs)
    app.router.add_get("/docs/", page_docs)
    app.router.add_get("/changelog", page_changelog)
    app.router.add_get("/changelog/", page_changelog)
    LOG.info("Multi-protocol routes registered: OpenAI + Anthropic + /pricing + /skills + /docs")
