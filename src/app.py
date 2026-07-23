"""
QueryPilot v2 - Streamlit UI
Chat with your database in plain English.

Enhancements over v1:
  - Multi-database switcher in sidebar
  - Query history panel (persistent across sessions)
  - EXPLAIN QUERY PLAN toggle
  - Configurable row limit slider
  - Auto-chart with more chart types (line, scatter, pie)
  - Export to CSV and JSON
  - Elapsed time display
  - Syntax-highlighted SQL with copy button (st.code)
  - Dark/light mode respect via Streamlit theming

Run:
    streamlit run src/app.py
"""

import json
import runpy
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent import (
    ask, get_schema, run_sql, has_llm_backend,
    load_history, list_databases,
    DEFAULT_DB, LLM_MODEL_OLLAMA,
    _USE_ANTHROPIC,
)

st.set_page_config(page_title="QueryPilot", page_icon="🧭", layout="wide")

# On a fresh deployment the demo DB is not in git (*.db is gitignored). Build it
# once from the committed generator so the app has real data to query on boot.
if not DEFAULT_DB.exists():
    setup = Path(__file__).resolve().parent.parent / "scripts" / "setup_db.py"
    if setup.exists():
        try:
            runpy.run_path(str(setup), run_name="__main__")
        except SystemExit:
            pass

# No API key and no local Ollama? Fall back to a demo that runs committed example
# queries live. The SQL is human-authored (labelled as such), but execution,
# results and charts are all real — nothing is a canned screenshot.
DEMO_MODE = not has_llm_backend()
EXAMPLES_FILE = DEFAULT_DB.parent / "examples.json"


@st.cache_data
def _load_examples() -> list[dict]:
    if EXAMPLES_FILE.exists():
        return json.loads(EXAMPLES_FILE.read_text(encoding="utf-8"))
    return []


# ── Helpers ───────────────────────────────────────────────────────
def _render_chart(df: pd.DataFrame, key: str):
    """Auto-chart heuristics with Plotly — more types than v1."""
    if df.empty or len(df.columns) < 2:
        return

    numeric_cols = df.select_dtypes("number").columns.tolist()
    text_cols = [c for c in df.columns if c not in numeric_cols]

    if not numeric_cols:
        return

    with st.expander("📊 Auto-chart", expanded=len(df) <= 50):
        chart_type = st.selectbox(
            "Chart type",
            ["Bar", "Line", "Pie", "Scatter"],
            key=f"ctype_{key}",
        )
        x_col = st.selectbox("X axis", df.columns.tolist(), key=f"x_{key}",
                              index=df.columns.get_loc(text_cols[0]) if text_cols else 0)
        y_col = st.selectbox("Y axis", numeric_cols, key=f"y_{key}")

        if chart_type == "Bar":
            fig = px.bar(df, x=x_col, y=y_col, color_discrete_sequence=["#1f2937"])
        elif chart_type == "Line":
            fig = px.line(df, x=x_col, y=y_col, markers=True, color_discrete_sequence=["#1f2937"])
        elif chart_type == "Pie":
            fig = px.pie(df, names=x_col, values=y_col)
        else:
            fig = px.scatter(df, x=x_col, y=y_col, color_discrete_sequence=["#1f2937"])

        fig.update_layout(plot_bgcolor="white", margin=dict(t=20, b=10), height=320)
        st.plotly_chart(fig, use_container_width=True)


def _render_export(df: pd.DataFrame, key: str):
    """Export buttons: CSV and JSON."""
    c1, c2 = st.columns(2)
    c1.download_button(
        "⬇️ CSV",
        df.to_csv(index=False).encode(),
        file_name="result.csv",
        mime="text/csv",
        key=f"csv_{key}",
    )
    c2.download_button(
        "⬇️ JSON",
        df.to_json(orient="records", indent=2).encode(),
        file_name="result.json",
        mime="application/json",
        key=f"json_{key}",
    )


# ── Sidebar ───────────────────────────────────────────────────────
with st.sidebar:
    st.title("🧭 QueryPilot v2")
    st.caption("Natural language → SQL → results")

    # Multi-DB switcher
    all_dbs = list_databases()
    if all_dbs:
        db_labels = {db.name: db for db in all_dbs}
        chosen_label = st.selectbox("🗄️ Database", list(db_labels.keys()),
                                    index=list(db_labels.keys()).index(DEFAULT_DB.name)
                                    if DEFAULT_DB.name in db_labels else 0)
        active_db = db_labels[chosen_label]
    else:
        active_db = DEFAULT_DB
        st.warning("No databases found. Run `python scripts/setup_db.py`.")

    st.divider()

    # Settings
    st.markdown("**Settings**")
    max_rows = st.slider("Max rows returned", 50, 1000, 500, step=50)
    include_explain = st.checkbox("Show query plan (EXPLAIN)", value=False)
    summarise = st.checkbox("Generate AI summary", value=True)

    st.divider()

    backend = "Anthropic Claude" if _USE_ANTHROPIC else f"Ollama ({LLM_MODEL_OLLAMA})"
    st.markdown(f"**LLM:** {backend}")

    st.divider()
    st.markdown("""
    **Try asking**
    - Show all customers with 3+ failed transactions in the last 30 days
    - Top 5 merchants by total transaction value this year
    - Monthly transaction volume trend
    - Which city has the highest failure rate?
    - Fraud-risk customers: high risk segment with UPI failures
    - Compare average transaction value by payment method
    """)

    st.divider()
    with st.expander("📋 Database schema"):
        if active_db.exists():
            st.code(get_schema(active_db), language="sql")
        else:
            st.warning("Database not found.")

    st.divider()
    if st.button("🗑️ Clear chat history"):
        st.session_state.history = []
        st.rerun()

# ── Header ────────────────────────────────────────────────────────
st.title("Ask your database anything")

if DEMO_MODE:
    st.info(
        "🧪 **Demo mode** — no LLM key or local Ollama detected, so live "
        "natural-language translation is off. Pick an example below: its SQL is "
        "pre-written, but it runs **live** against the database and the results and "
        "charts are real. Add an `ANTHROPIC_API_KEY` or run Ollama to ask your own "
        "questions in plain English.",
        icon="🧪",
    )

if not active_db.exists():
    st.error(f"Database `{active_db.name}` not found. Run `python scripts/setup_db.py` first.")
    st.stop()

# ── Tabs: Chat | Query History ────────────────────────────────────
tab_chat, tab_history = st.tabs(["💬 Chat", "📜 Query History"])

# ── Chat Tab ──────────────────────────────────────────────────────
with tab_chat:
    if "history" not in st.session_state:
        st.session_state.history = []

    # Render past messages
    for item in st.session_state.history:
        with st.chat_message("user"):
            st.markdown(item["question"])
        with st.chat_message("assistant"):
            if item.get("corrected"):
                st.caption("⚙️ Auto-corrected")
            if item.get("summary"):
                st.markdown(f"**{item['summary']}**")
            st.code(item["sql"], language="sql")
            if item.get("explain_plan"):
                with st.expander("🔍 Query Plan"):
                    st.code(item["explain_plan"])
            if item.get("error"):
                st.error(item["error"])
            elif item.get("df") is not None:
                df = item["df"]
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption(f"{len(df)} rows · {item.get('ms', 0)} ms")
                _render_chart(df, key=f"chart_{item.get('_idx', 0)}")
                _render_export(df, key=f"exp_{item.get('_idx', 0)}")

    # Input. In demo mode the user picks a pre-written example instead of typing,
    # since there is no backend to translate free text. Both paths run the SQL
    # through the identical safety + execution code below.
    question = None
    demo_sql = None
    if DEMO_MODE:
        examples = _load_examples()
        labels = ["— pick an example query —"] + [e["question"] for e in examples]
        picked = st.selectbox("Example questions", labels, key="demo_pick")
        if picked and picked != labels[0]:
            question = picked
            demo_sql = next(e["sql"] for e in examples if e["question"] == picked)
    else:
        question = st.chat_input("e.g. Top 10 merchants by failed transaction rate")

    if question:
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Querying…"):
                if demo_sql is not None:
                    result = run_sql(demo_sql, db_path=active_db, max_rows=max_rows)
                    result.question = question
                else:
                    result = ask(
                        question,
                        summarise=summarise,
                        db_path=active_db,
                        max_rows=max_rows,
                        include_explain=include_explain,
                    )

            entry = {
                "question": question,
                "sql": result.sql,
                "corrected": result.corrected,
                "ms": result.execution_ms,
                "_idx": len(st.session_state.history),
            }

            if result.corrected:
                st.caption("⚙️ First attempt failed — auto-corrected")

            if result.summary:
                st.markdown(f"**{result.summary}**")
                entry["summary"] = result.summary

            st.code(result.sql, language="sql")

            if result.explain_plan:
                with st.expander("🔍 Query Plan"):
                    st.code(result.explain_plan)
                entry["explain_plan"] = result.explain_plan

            if result.error:
                st.error(f"Query failed: {result.error}")
                entry["error"] = result.error
                entry["df"] = None
            else:
                df = pd.DataFrame(result.rows, columns=result.columns)
                entry["df"] = df
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption(f"{len(df)} rows · {result.execution_ms} ms")
                _render_chart(df, key=f"newchart_{len(st.session_state.history)}")
                _render_export(df, key=f"newexp_{len(st.session_state.history)}")

            st.session_state.history.append(entry)

# ── History Tab ───────────────────────────────────────────────────
with tab_history:
    st.subheader("Persistent Query History")
    hist = load_history()
    if not hist:
        st.info("No queries yet. Start chatting!")
    else:
        df_hist = pd.DataFrame(hist[::-1])  # newest first
        df_hist = df_hist[["ts", "question", "rows", "execution_ms", "error", "corrected", "db"]]
        df_hist.columns = ["Timestamp", "Question", "Rows", "Time (ms)", "Error", "Corrected", "DB"]
        df_hist["Error"] = df_hist["Error"].fillna("—")
        st.dataframe(df_hist, use_container_width=True, hide_index=True)

        st.download_button(
            "⬇️ Download history as JSON",
            json.dumps(hist, indent=2).encode(),
            file_name="query_history.json",
            mime="application/json",
        )
