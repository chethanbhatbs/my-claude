"""Regression tests for My-Claude.

These exist because every bug found on 2026-08-14 was a *silent truncation* that still rendered a
plausible-looking page: a 500-message cap in the detail pane, a 400-row cap in the history list,
tool-only transcripts dropped from the index, the skills page defaulting to a filter that hid 689 of
698 skills, and an activity grid that stopped 6 days before today. Nothing failed loudly, so nothing
caught them. Each test below asserts "we show everything we found", not "the code ran".

Run:  .venv/bin/python -m pytest tests -q
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import myclaude as mc  # noqa: E402

CLAUDE = Path.home() / ".claude"
PROJECTS = CLAUDE / "projects"
pytestmark = pytest.mark.skipif(not PROJECTS.exists(), reason="no ~/.claude/projects on this machine")


# ---------- history: nothing dropped, nothing capped ----------

def test_index_lists_every_top_level_transcript():
    """Every *.jsonl directly under a project dir must appear, including tool-only ones (n == 0)."""
    on_disk = {str(p) for p in PROJECTS.glob("*/*.jsonl")}
    indexed = {c["path"] for c in mc.conversation_index()}
    assert on_disk - indexed == set(), "transcripts on disk missing from the index"


def test_index_with_sub_lists_every_nested_transcript():
    on_disk = {str(p) for p in PROJECTS.rglob("*.jsonl")}
    indexed = {c["path"] for c in mc.conversation_index(True)}
    assert on_disk - indexed == set(), "nested sub-agent/workflow transcripts missing from the index"


def test_every_conversation_has_a_preview_and_kind():
    for c in mc.conversation_index():
        assert c["preview"], f"empty preview for {c['path']}"
        assert c["kind"] in ("conversation", "sub-agent", "workflow"), c["kind"]


def test_detail_pane_returns_all_messages_of_the_longest_conversation():
    """The old max_messages=500 default silently cut 12 conversations short."""
    convs = mc.conversation_index()
    if not convs:
        pytest.skip("no conversations")
    biggest = max(convs, key=lambda c: c["n"])
    msgs = mc.parse_transcript_messages(biggest["path"])
    assert len(msgs) >= biggest["n"], "detail pane returned fewer messages than the index counted"
    assert len(msgs) != 500, "suspicious: exactly 500 messages looks like a cap"


def test_history_route_lists_every_conversation_it_counted():
    """The rendered list must contain a row per conversation in the filtered set (no [:400] cap)."""
    from fastapi.testclient import TestClient
    html = TestClient(mc.app).get("/p/history").text
    rows = html.count('class="conv-item"')
    on_disk = len(list(PROJECTS.glob("*/*.jsonl")))
    assert rows >= on_disk, f"history rendered {rows} rows for {on_disk} on-disk transcripts"


# ---------- archived sessions recovered from history.jsonl ----------

@pytest.mark.skipif(not (CLAUDE / "history.jsonl").exists(), reason="no history.jsonl")
def test_archived_sessions_are_only_the_ones_without_a_transcript():
    on_disk = {p.stem for p in PROJECTS.rglob("*.jsonl")}
    arch = mc.archived_sessions()
    assert all(a["session"] not in on_disk for a in arch), "archived list overlaps live transcripts"
    assert all(a["n"] > 0 for a in arch), "archived session with no prompts"
    assert all(a["path"].startswith("archived:") for a in arch)


@pytest.mark.skipif(not (CLAUDE / "history.jsonl").exists(), reason="no history.jsonl")
def test_archived_prompt_counts_match_the_summary():
    arch = mc.archived_sessions()
    if not arch:
        pytest.skip("nothing archived")
    a = max(arch, key=lambda x: x["n"])
    assert len(mc.archived_prompts(a["session"])) == a["n"]


# ---------- usage: numbers must be reproducible from the raw files ----------

def test_usage_cost_matches_a_per_row_recompute():
    """Guards the vectorised maths in aggregate_usage against drift from the plain per-row formula."""
    df = mc.aggregate_usage()
    if df.empty:
        pytest.skip("no usage data")
    per_row = sum(
        mc.cost_for(r.model, r.input_tokens, r.output_tokens, r.cache_creation, r.cache_read)
        for r in df.itertuples()
    )
    assert abs(per_row - float(df["cost"].sum())) < 1e-6


def test_usage_disk_cache_round_trips_losslessly():
    """parse_transcript_usage reads through .mc_usage_cache.json; it must equal a fresh scan."""
    files = sorted(PROJECTS.glob("*/*.jsonl"))[:25]
    if not files:
        pytest.skip("no transcripts")
    for f in files:
        assert mc.parse_transcript_usage(str(f)) == mc._scan_usage(str(f)), f"cache drift for {f.name}"


def test_usage_counts_nested_transcripts_too():
    """Sub-agent/workflow turns are metered too and must not be silently excluded.

    Asserted per transcript rather than on a global total: a live session appends to its own
    transcript while the test runs, so global counts are inherently racy.
    """
    mc.clear_cache()
    df = mc.aggregate_usage()
    if df.empty:
        pytest.skip("no usage data")
    assert {"kind", "transcript"} <= set(df.columns)
    nested = [p for p in PROJECTS.rglob("*.jsonl") if len(p.relative_to(PROJECTS).parts) > 2]
    nested_with_usage = {p.stem for p in nested if mc._scan_usage(str(p))}
    if not nested_with_usage:
        pytest.skip("no nested transcripts with usage")
    missing = nested_with_usage - set(df["transcript"])
    assert not missing, f"{len(missing)} sub-agent/workflow transcripts absent from the usage aggregate"
    # and a transcript that is definitely not being written to must match exactly
    stable = min(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
    assert len(df[df["transcript"] == stable.stem]) == len(mc._scan_usage(str(stable)))


# ---------- activity grid ----------

def test_heatmap_window_ends_today_and_is_53_full_weeks():
    w = mc.heatmap_window()
    assert w["end"] == date.today().isoformat(), "activity grid does not reach today"
    start = date.fromisoformat(w["start"])
    assert start.weekday() == 6, "grid must start on a Sunday so columns are whole weeks"
    assert (date.fromisoformat(w["end"]) - start).days <= w["weeks"] * 7 - 1


def test_activity_heatmap_carries_billable_tokens_not_turn_counts():
    """The grid measures tokens/day; turns/day treated a day of 3 huge Opus turns as tiny."""
    from fastapi.testclient import TestClient
    html = TestClient(mc.app).get("/p/usage").text
    m = re.search(r"const heat=(\[.*?\]);", html, re.S)
    if not m:
        pytest.skip("no heatmap on the page")
    heat = json.loads(m.group(1))
    if not heat:
        pytest.skip("no dated usage")
    df = mc.aggregate_usage()
    billable = df["input_tokens"] + df["cache_creation"] + df["cache_read"] + df["output_tokens"]
    per_day = billable.groupby(df["date"]).sum()
    for h in heat[:5]:
        expected = int(per_day[date.fromisoformat(h["date"])])
        assert h["v"] == expected, f"{h['date']}: grid says {h['v']}, tokens are {expected}"
    assert max(h["v"] for h in heat) > 1000, "values look like turn counts, not tokens"
    assert "billable tokens/day" in html


@pytest.mark.parametrize("d", ["2026-08-14", "2026-08-09", "2026-08-15", "2026-01-01"])
def test_heatmap_window_covers_the_given_day_whatever_the_weekday(d):
    day = date.fromisoformat(d)
    w = mc.heatmap_window(day)
    assert date.fromisoformat(w["start"]) <= day <= date.fromisoformat(w["end"])
    assert w["end"] == d


# ---------- skills page defaults ----------

def test_skills_page_shows_the_whole_library_by_default():
    """"Recent" (edited in the last 7 days) used to default ON and hid 689 of 698 skills."""
    from fastapi.testclient import TestClient
    html = TestClient(mc.app).get("/p/skills").text
    total = len(mc.load_skills())
    assert f"<strong>{total}</strong> skills" in html, "skills page is filtering by default"


def test_skills_default_sort_puts_recently_used_first():
    from fastapi.testclient import TestClient
    html = TestClient(mc.app).get("/p/skills").text
    assert "<option selected>Recently used" in html


def test_skill_last_used_carries_a_time_not_just_a_date():
    usage = mc.skill_usage()
    used = [v["last_ts"] for v in usage.values() if v["last_ts"]]
    if not used:
        pytest.skip("no skill usage recorded")
    assert len(mc.fmt_when(max(used)).split(" ")) == 2, "last-used should be date + time"


# ---------- live plan limits ----------

def test_limits_normaliser_shapes_the_api_payload():
    """Uses a captured payload, so it doesn't need the network or the keychain."""
    payload = {
        "limits": [
            {"kind": "session", "group": "session", "percent": 18, "severity": "normal",
             "resets_at": "2026-08-14T11:30:00+00:00", "scope": None, "is_active": False},
            {"kind": "weekly_all", "group": "weekly", "percent": 45, "severity": "normal",
             "resets_at": "2026-08-17T11:00:00+00:00", "scope": None, "is_active": True},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "severity": "normal",
             "resets_at": None, "scope": {"model": {"display_name": "Fable"}}, "is_active": False},
        ],
        "extra_usage": {"is_enabled": False},
        "spend": {"enabled": False, "percent": 0, "used": {"amount_minor": 0, "currency": "USD"}},
    }
    out = mc._norm_limits(payload, "live", 1786707502.0)
    assert out["ok"] and out["source"] == "live"
    labels = [r["label"] for r in out["rows"]]
    assert labels[0] == "Current session"
    assert "All models" in labels and "Fable" in labels
    fable = next(r for r in out["rows"] if r["label"] == "Fable")
    assert fable["unused"] is True, "0% with no reset time should read as 'not used yet'"
    assert next(r for r in out["rows"] if r["label"] == "All models")["pct"] == 45.0


def test_limits_endpoint_never_500s_and_says_where_it_got_the_numbers():
    from fastapi.testclient import TestClient
    r = TestClient(mc.app).get("/api/usage-limits")
    assert r.status_code == 200
    assert r.json()["source"] in ("live", "cache", "none")


def test_no_credential_is_ever_written_to_a_cache_file():
    """The keychain token lives in memory only."""
    for f in (mc.CACHE_FILE, mc.USAGE_CACHE_FILE):
        if f.exists():
            assert "accessToken" not in f.read_text(), f"token leaked into {f.name}"


# ---------- full-text search index ----------

def test_search_index_builds_and_covers_every_transcript():
    r = mc.build_search_index()
    if not r.get("ok"):
        pytest.skip(r.get("reason", "index unavailable"))
    con = mc._search_conn()
    try:
        indexed = {row[0] for row in con.execute("SELECT path FROM files")}
    finally:
        con.close()
    on_disk = {str(p) for p in PROJECTS.rglob("*.jsonl")}
    assert on_disk - indexed == set(), "transcripts missing from the search index"


def test_search_finds_a_string_that_is_really_in_a_transcript():
    if not mc.search_available():
        pytest.skip("no FTS5")
    convs = mc.conversation_index()
    if not convs:
        pytest.skip("no conversations")
    # look across several messages, not just the first: a conversation can open with a pasted URL
    # that contains no plain word, which used to make this test skip itself into uselessness
    word = target = None
    for c in sorted(convs, key=lambda c: -c["n"])[:5]:
        for m in mc.parse_transcript_messages(c["path"])[:40]:
            cand = [w for w in m["text"].split() if w.isalpha() and 7 <= len(w) <= 20]
            if cand:
                word, target = cand[0], c
                break
        if word:
            break
    if not word:
        pytest.skip("no searchable word found in any transcript")
    hits = mc.search_messages(word)
    assert any(h["path"] == target["path"] for h in hits), f"'{word}' not found in its own transcript"
    assert all(isinstance(h["idx"], int) for h in hits), "hits must carry a message index for deep links"


@pytest.mark.parametrize("q", ['claude "exact phrase"', "a.b.c", "trailing-", '"', "*", "AND OR NOT", "  "])
def test_search_never_raises_on_messy_input(q):
    if not mc.search_available():
        pytest.skip("no FTS5")
    assert isinstance(mc.search_messages(q), list)


# ---------- every page renders ----------

@pytest.mark.parametrize("slug", ["overview", "skills", "mcp-plugins", "usage", "history", "system", "help"])
def test_pages_render(slug):
    from fastapi.testclient import TestClient
    r = TestClient(mc.app).get(f"/p/{slug}")
    assert r.status_code == 200 and "<main" in r.text


# ---------- live sessions ----------

def test_live_endpoint_shape_and_window():
    from fastapi.testclient import TestClient
    r = TestClient(mc.app).get("/api/live")
    assert r.status_code == 200
    j = r.json()
    assert {"processes", "sessions", "totals", "window_min"} <= set(j)
    for s in j["sessions"]:
        assert s["idle_s"] <= mc.LIVE_WINDOW, "a session outside the live window was listed as active"
        assert s["turns"] > 0 and s["tokens"] > 0
        assert s["context"] > 0, "context size is what makes this panel useful"
    for p in j["processes"]:
        assert p["tty"] not in ("??", "?"), "daemon/bg helpers must not be listed as terminal sessions"


def test_live_sessions_tokens_match_the_transcript():
    sess = mc.live_sessions()
    if not sess:
        pytest.skip("nothing active right now")
    s = sess[0]
    rows = mc._scan_usage(s["path"])
    billable = sum(r["input_tokens"] + r["output_tokens"] + r["cache_creation"] + r["cache_read"] for r in rows)
    assert abs(billable - s["tokens"]) / max(1, billable) < 0.02, "live token count drifted from the file"


# ---------- token-reduction insights ----------

def test_insights_are_computed_not_canned():
    ins = mc.token_insights()
    assert ins, "no insights produced"
    for i in ins:
        assert i["metric"] and i["title"] and i["action"], i
        assert i["severity"] in ("normal", "high")
    titles = " ".join(i["title"] for i in ins)
    assert "skill catalogue" in titles.lower()


def test_skill_catalogue_insight_matches_an_independent_count():
    skills = mc.load_skills()
    chars = sum(len(s["name"]) + len(s["description"]) for s in skills)
    expected = mc.fmt_num(chars // mc.CHARS_PER_TOKEN) + " tokens"
    ins = next(i for i in mc.token_insights() if "catalogue" in i["title"].lower())
    assert ins["metric"] == expected, f"{ins['metric']} != {expected}"


def test_repeated_reads_never_reports_a_single_read_as_redundant():
    d = mc.repeated_reads()
    assert d["redundant"] >= 0
    for w in d["worst"]:
        assert w["count"] >= 5, "worst list should only hold files read 5+ times"


# ---------- curated links ----------

def test_links_file_is_valid_and_https_only():
    links = mc.load_links()
    assert links["sections"], "links.json produced no sections"
    seen = set()
    for sec in links["sections"]:
        assert sec.get("title") and sec.get("links")
        for l in sec["links"]:
            assert l["url"].startswith("https://"), l["url"]
            assert l["url"] not in seen, f"duplicate link {l['url']}"
            seen.add(l["url"])
            assert l.get("title")
    assert len(seen) >= 15


def test_help_page_renders_the_links():
    from fastapi.testclient import TestClient
    html = TestClient(mc.app).get("/p/help").text
    n = sum(len(s["links"]) for s in mc.load_links()["sections"])
    assert html.count('class="linkrow"') == n


def test_live_rows_carry_a_readable_conversation_path():
    """The View button reads the conversation in-app, so every live row needs a real transcript."""
    for s in mc.live_sessions():
        assert Path(s["path"]).exists(), s["path"]
        from fastapi.testclient import TestClient
        r = TestClient(mc.app).get("/api/conversation", params={"path": s["path"]})
        assert r.status_code == 200 and 'id="convMsgs"' in r.text


def test_conversation_partial_can_close_itself_in_a_modal():
    convs = mc.conversation_index()
    if not convs:
        pytest.skip("no conversations")
    from fastapi.testclient import TestClient
    html = TestClient(mc.app).get("/api/conversation", params={"path": convs[0]["path"]}).text
    assert 'class="close modal-only"' in html, "no close button when opened as a modal"


def test_both_new_sections_are_collapsible():
    from fastapi.testclient import TestClient
    c = TestClient(mc.app)
    ov = c.get("/p/overview").text
    assert '<details class="live" id="liveBox"' in ov, "Active now must be collapsible"
    assert '<details class="procs">' in ov, "the process list must be collapsed by default"
    us = c.get("/p/usage").text
    assert '<details class="fold" id="insFold"' in us, "insights must be collapsible"


def test_live_processes_carry_sortable_uptime():
    for p in mc.live_processes():
        assert p["uptime_s"] >= 0 and p["uptime"], p


def test_live_rows_are_told_apart_by_a_real_title():
    """Every home-directory session rendered as "~ (home)" before, so three different
    conversations looked like one."""
    sess = mc.live_sessions()
    if len(sess) < 1:
        pytest.skip("nothing active")
    for s in sess:
        assert s["title"] and s["title"] != s["project"], s
        assert len(s["title"]) <= 76
    if len(sess) > 1:
        assert len({s["title"] for s in sess}) == len(sess), "live rows share a title"


def test_only_typed_messages_are_labelled_user():
    """Claude Code stores notifications, reminders, command output and SDK prompts as type=user.
    Labelling them USER told the person they wrote text they never typed."""
    r = mc.message_role
    assert r({"type": "user", "origin": {"kind": "human"}}, "hello") == "user"
    assert r({"type": "user"}, "old record typed before origin existed") == "user"
    assert r({"type": "user", "origin": {"kind": "human"}}, "<command-message>x</command-message>") == "user"
    assert r({"type": "assistant"}, "hi") == "assistant"
    assert r({"type": "user", "origin": {"kind": "task-notification"}}, "<task-notification>") == "notification"
    assert r({"type": "user"}, "<task-notification>\n<task-id>x</task-id>") == "notification"
    assert r({"type": "user", "isMeta": True}, "Base directory for this skill: /x") == "system"
    assert r({"type": "user", "isCompactSummary": True}, "This session is being continued") == "system"
    assert r({"type": "user", "origin": {"kind": "peer"}, "isMeta": True}, "msg") == "system"
    assert r({"type": "user", "origin": {"kind": "peer"}}, "msg") == "peer"
    assert r({"type": "user", "origin": {"kind": "auto-continuation"}}, "go") == "system"
    assert r({"type": "user"}, "<local-command-stdout>ok</local-command-stdout>") == "output"
    assert r({"type": "user"}, "<bash-stdout>ok</bash-stdout>") == "output"
    assert r({"type": "user"}, "[Request interrupted by user]") == "system"
    assert r({"type": "user", "entrypoint": "sdk-ts"}, "Condense the tool payload") == "script"


def test_retention_reads_default_and_saves_only_that_key(tmp_path, monkeypatch):
    """The History "Keep conversations" setting writes cleanupPeriodDays into ~/.claude/settings.json.
    It must leave every other key alone, back the file up, and refuse values Claude Code rejects (0)."""
    monkeypatch.setattr(mc, "CLAUDE", tmp_path)
    monkeypatch.setattr(mc, "known_project_dirs", lambda: [])
    monkeypatch.setattr(mc, "MANAGED_SETTINGS", tmp_path / "no-managed.json")
    info = mc.retention_info()
    assert (info["days"], info["set"], info["error"], info["others"]) == (30, False, None, [])
    sp = tmp_path / "settings.json"
    sp.write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}))
    mc.set_retention(3650)
    saved = json.loads(sp.read_text())
    assert saved == {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}, "cleanupPeriodDays": 3650}
    assert json.loads((tmp_path / "settings.json.my-claude.bak").read_text()) == {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}
    assert (mc.retention_info()["days"], mc.retention_info()["set"]) == (3650, True)
    for bad in (0, -5, "90", 1.5, True, None):
        with pytest.raises(ValueError):
            mc.set_retention(bad)
    assert json.loads(sp.read_text())["cleanupPeriodDays"] == 3650
    sp.write_text("{not json")
    assert mc.retention_info()["error"]


def test_overview_tokens_card_opens_usage_on_the_same_30_day_window():
    """The card says 30d; clicking it must land on Usage with 30 days picked, at Measured usage."""
    src = Path(mc.__file__).read_text()
    assert '"label": "Tokens · 30d", "value": tokens30, "sub": cost30, "href": "/p/usage?win=30+days#measured"' in src
    assert 'id="measured"' in (Path(mc.__file__).parent / "templates" / "token.html").read_text()


def test_mcp_check_counts_a_server_that_answers_4xx_as_reachable(monkeypatch):
    """A remote MCP server answers a plain GET with 401/403/405. That server is up; only 5xx and
    timeouts mean it is down. Counting 4xx as down made "2 of 10 reachable" when 5 answered."""
    import urllib.request, urllib.error
    def fake(code):
        def _open(req, timeout=0): raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return _open
    for code, want in ((401, "ok"), (403, "ok"), (405, "ok"), (502, "fail"), (503, "fail")):
        monkeypatch.setattr(urllib.request, "urlopen", fake(code))
        assert mc.mcp_check("s", {"url": "https://example.invalid/mcp"})[0] == want, code


def test_retention_shows_a_project_value_lower_than_the_user_value(tmp_path, monkeypatch):
    """anthropics/claude-code#90148: a project's cleanupPeriodDays reportedly controls the sweep
    machine-wide, so the dialog must surface it instead of only reading ~/.claude/settings.json."""
    claude = tmp_path / ".claude"; claude.mkdir(); proj = tmp_path / "proj"; (proj / ".claude").mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 3650}))
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"cleanupPeriodDays": 30}))
    monkeypatch.setattr(mc, "CLAUDE", claude); monkeypatch.setattr(mc, "HOME", tmp_path)
    monkeypatch.setattr(mc, "known_project_dirs", lambda: [str(proj)])
    monkeypatch.setattr(mc, "MANAGED_SETTINGS", tmp_path / "none.json")
    info = mc.retention_info()
    assert info["days"] == 3650 and info["lowest"] == 30
    assert [o["file"] for o in info["others"]] == ["~/proj/.claude/settings.local.json"]


def test_backup_copies_new_and_changed_transcripts_and_never_deletes(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"; pj = claude / "projects" / "-x"; pj.mkdir(parents=True)
    monkeypatch.setattr(mc, "CLAUDE", claude); monkeypatch.setattr(mc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(mc, "BACKUP_DIR", tmp_path / "data" / "bk"); monkeypatch.setattr(mc, "DATA_SETTINGS", tmp_path / "data" / "s.json")
    (pj / "a.jsonl").write_text("1\n"); (pj / "b.jsonl").write_text("2\n")
    assert mc.backup_transcripts()["copied"] == 2
    assert mc.backup_transcripts()["copied"] == 0          # unchanged files are skipped
    (pj / "a.jsonl").write_text("1\n1b\n"); (pj / "b.jsonl").unlink()
    assert mc.backup_transcripts()["copied"] == 1
    assert (tmp_path / "data" / "bk" / "-x" / "b.jsonl").read_text() == "2\n"   # deleted upstream, kept here
    assert (tmp_path / "data" / "bk" / "-x" / "a.jsonl").read_text() == "1\n1b\n"


def test_titles_prefer_rename_then_claude_name_then_first_prompt(tmp_path):
    f = tmp_path / "s.jsonl"
    rows = [{"type": "user", "timestamp": "2026-10-01T00:00:00Z", "message": {"role": "user", "content": "first prompt here"}},
            {"type": "ai-title", "aiTitle": "AI name"}]
    f.write_text("\n".join(json.dumps(r) for r in rows))
    assert mc._scan_meta(str(f))["title"] == "AI name"
    f.write_text("\n".join(json.dumps(r) for r in rows + [{"type": "custom-title", "customTitle": "My name"}]))
    assert mc._scan_meta(str(f))["title"] == "My name"
    f.write_text(json.dumps(rows[0])); m = mc._scan_meta(str(f))
    assert m["title"] == "" and m["preview"] == "first prompt here"


def test_compaction_is_marked_in_the_conversation(tmp_path):
    f = tmp_path / "s.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "before"}, "origin": {"kind": "human"}},
            {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "auto", "preTokens": 635278}},
            {"type": "user", "message": {"role": "user", "content": "after"}, "origin": {"kind": "human"}}]
    f.write_text("\n".join(json.dumps(r) for r in rows))
    msgs = mc.parse_transcript_messages(str(f))
    assert [m["role"] for m in msgs] == ["user", "compact", "user"]
    assert "auto" in msgs[1]["text"] and "635" in msgs[1]["text"]
    assert mc._scan_meta(str(f))["compactions"] == 1


def test_labels_save_clean_and_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "DATA_DIR", tmp_path); monkeypatch.setattr(mc, "LABELS_PATH", tmp_path / "labels.json")
    lab = mc.save_label("abc-123", "  Payment   retry ", ["#Billing", "bug", "bug", "x y"])
    assert lab == {"name": "Payment retry", "tags": ["billing", "bug", "xy"]}
    assert mc.load_labels()["abc-123"]["name"] == "Payment retry"
    mc.save_label("abc-123", "", [])
    assert "abc-123" not in mc.load_labels()
    with pytest.raises(ValueError):
        mc.save_label("../etc", "x", [])


def test_usage_export_matches_the_window_and_sums_per_day(monkeypatch):
    import pandas as pd
    today = datetime.now().date(); old = today - timedelta(days=40)
    df = pd.DataFrame([
        {"date": today, "label": "opus 5.5", "model": "m", "input_tokens": 1, "cache_creation": 2, "cache_read": 3, "output_tokens": 4, "cost": 0.5},
        {"date": today, "label": "opus 5.5", "model": "m", "input_tokens": 1, "cache_creation": 2, "cache_read": 3, "output_tokens": 4, "cost": 0.5},
        {"date": old, "label": "opus 5.5", "model": "m", "input_tokens": 9, "cache_creation": 9, "cache_read": 9, "output_tokens": 9, "cost": 9.0}])
    monkeypatch.setattr(mc, "aggregate_usage", lambda: df)
    rows = json.loads(mc.export_usage(fmt="json", win="30 days").body)
    assert rows == [{"date": str(today), "model": "opus 5.5", "turns": 2, "input_tokens": 2, "cache_write_tokens": 4,
                     "cache_read_tokens": 6, "output_tokens": 8, "cost_usd_est": 1.0}]
    csv_text = mc.export_usage(fmt="csv", win="All time").body.decode()
    assert csv_text.splitlines()[0] == "date,model,turns,input_tokens,cache_write_tokens,cache_read_tokens,output_tokens,cost_usd_est"
    assert len(csv_text.strip().splitlines()) == 3


def _usage_df(rows):
    import pandas as pd
    base = {"model": "claude-opus-5-5", "label": "opus 5.5", "input_tokens": 0, "cache_creation": 0, "cache_read": 0,
            "output_tokens": 0, "cost": 0.0, "kind": "conversation"}
    return pd.DataFrame([dict(base, **r) for r in rows])


def test_pace_forecast_projects_a_straight_line():
    from datetime import timezone
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    half = {"group": "session", "resets_at": (now + timedelta(hours=2.5)).isoformat()}
    assert mc.pace_forecast(dict(half, pct=20), now) == "At this pace: about 40% by reset"
    assert mc.pace_forecast(dict(half, pct=60), now).startswith("At this pace: 100% around")
    assert mc.pace_forecast(dict(half, pct=0), now) == ""
    early = {"group": "session", "resets_at": (now + timedelta(hours=4, minutes=55)).isoformat(), "pct": 5}
    assert mc.pace_forecast(early, now) == ""          # under 10 minutes in: too early to say


def test_window_breakdown_counts_only_turns_inside_the_window(monkeypatch):
    from datetime import timezone
    monkeypatch.setattr(mc, "_titles", lambda: {"a": ("Alpha", "/p/a.jsonl")})
    df = _usage_df([
        {"session": "a", "transcript": "a", "ts": "2026-10-27T10:00:00Z", "cache_read": 300},
        {"session": "a", "transcript": "sub1", "kind": "sub-agent", "ts": "2026-10-27T10:05:00Z", "cache_read": 100},
        {"session": "b", "transcript": "b", "ts": "2026-10-27T11:00:00Z", "cache_read": 100},
        {"session": "b", "transcript": "b", "ts": "2026-10-20T11:00:00Z", "cache_read": 9999}])
    b = mc.window_breakdown(datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc), df)
    assert b["total"] == 500
    assert [(r["session"], r["tokens"], round(r["share"]), r["subagents"]) for r in b["rows"]] == [("a", 400, 80, 1), ("b", 100, 20, 0)]
    assert b["rows"][0]["title"] == "Alpha"


def test_cache_rebuilds_only_count_writes_after_a_pause(monkeypatch):
    monkeypatch.setattr(mc, "_titles", lambda: {})
    df = _usage_df([
        {"session": "a", "transcript": "a", "ts": "2026-10-27T10:00:00Z", "cache_creation": 1000},
        {"session": "a", "transcript": "a", "ts": "2026-10-27T10:01:00Z", "cache_creation": 1000},   # 1 min: not a pause
        {"session": "a", "transcript": "a", "ts": "2026-10-27T10:20:00Z", "cache_creation": 4000},   # 19 min: counted
        {"session": "a", "transcript": "a", "ts": "2026-10-27T11:00:00Z", "cache_creation": 0}])     # pause but no write
    r = mc.cache_rebuilds(df)
    assert (r["turns"], r["tokens"]) == (1, 4000)
    assert r["by_session"][0]["longest_gap_min"] == 19
    assert abs(r["cost"] - 4000 * mc.pricing_for("claude-opus-5-5")["cache_write"] / 1e6) < 1e-9


def test_limit_alerts_fire_once_per_level_per_window(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "DATA_DIR", tmp_path); monkeypatch.setattr(mc, "DATA_SETTINGS", tmp_path / "s.json")
    sent = []; monkeypatch.setattr(mc, "notify_desktop", lambda t, x: sent.append(x))
    rows = [{"kind": "session", "label": "Current session", "pct": 85, "resets_at": "2026-10-27T17:00:00Z"}]
    assert len(mc.check_limit_alerts(rows)) == 1
    assert mc.check_limit_alerts(rows) == []                       # same window, same level: silent
    rows[0]["pct"] = 96
    assert len(mc.check_limit_alerts(rows)) == 1                   # 95% is its own alert
    rows[0]["resets_at"] = "2026-10-27T22:00:00Z"
    assert len(mc.check_limit_alerts(rows)) == 2                   # new window: both levels again
    assert len(sent) == 4


def test_subagent_timeline_places_each_run(monkeypatch):
    df = _usage_df([
        {"session": "a", "transcript": "a", "ts": "2026-10-27T10:00:00Z", "cache_read": 5},
        {"session": "a", "transcript": "s1", "kind": "sub-agent", "ts": "2026-10-27T10:00:00Z", "cache_read": 100},
        {"session": "a", "transcript": "s1", "kind": "sub-agent", "ts": "2026-10-27T10:10:00Z", "cache_read": 100},
        {"session": "a", "transcript": "s2", "kind": "sub-agent", "ts": "2026-10-27T10:10:00Z", "cache_read": 50},
        {"session": "a", "transcript": "s2", "kind": "sub-agent", "ts": "2026-10-27T10:20:00Z", "cache_read": 50}])
    monkeypatch.setattr(mc, "aggregate_usage", lambda: df)
    r = mc._sub_rows("a")
    assert r["tokens"] == 300
    assert [(x["id"], x["left"], x["width"], x["tokens"]) for x in r["rows"]] == [("s1", 0.0, 50.0, 200), ("s2", 50.0, 50.0, 100)]
    assert len(r["groups"]) == 1


def test_subagent_runs_far_apart_get_their_own_time_axis(monkeypatch):
    df = _usage_df([
        {"session": "a", "transcript": "s1", "kind": "sub-agent", "ts": "2026-08-17T10:00:00Z", "cache_read": 1},
        {"session": "a", "transcript": "s1", "kind": "sub-agent", "ts": "2026-08-17T10:10:00Z", "cache_read": 1},
        {"session": "a", "transcript": "s2", "kind": "sub-agent", "ts": "2026-10-01T09:00:00Z", "cache_read": 1},
        {"session": "a", "transcript": "s2", "kind": "sub-agent", "ts": "2026-10-01T09:20:00Z", "cache_read": 1}])
    monkeypatch.setattr(mc, "aggregate_usage", lambda: df)
    r = mc._sub_rows("a")
    assert len(r["groups"]) == 2 and all(x["width"] == 100.0 for x in r["rows"])


def test_pause_lengths_read_in_days_and_hours():
    assert [mc.fmt_minutes(m) for m in (7, 60, 95, 19590)] == ["7 min", "1 h", "1 h 35 min", "13 d 14 h"]
