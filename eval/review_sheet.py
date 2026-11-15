"""A worksheet for the two human reviewers of the semantic blind packet.

The packet already contains everything a reviewer needs except a way to return
a file without retyping it. Each of the 24 lines in ``reviewer-A.jsonl`` carries
a 64-character sample hash and a 64-character rubric hash; one mistyped nibble
and ``analyze_reviews.py`` rejects the whole file after the review is finished.
That is the failure this module exists to remove.

Two rules shape it.

**It does not label.** The verdict radios start empty, there is no default, and
``check`` reports an incomplete file as a problem instead of filing blanks. A
worksheet that guessed would be a model answering its own exam.

**It does not invent evidence.** Hashes are copied from ``samples.json``, never
transcribed; the document is rendered with an explicit line gutter so
``evidence_lines`` can cite a position that actually exists. ``build`` re-derives
every hash before writing a page, so a packet edited after the fact refuses to
render rather than producing a worksheet that would later be rejected.

Run ``python -m eval.review_sheet build --packet <dir> --slot A`` and hand the
resulting page, and nothing else, to the reviewer.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import sys

VERDICTS = ("pass", "fail", "inconclusive")
SAMPLE_FIELDS = ("sample_id", "sample_sha256", "rubric_sha256", "reviewer_id",
                 "verdict", "evidence_lines", "note")


class PacketInvalid(ValueError):
    """The packet cannot be rendered as it stands."""


def digest(value) -> str:
    """The hash ``analyze_reviews.py`` computes, byte for byte."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), default=str).encode()).hexdigest()


def load(folder: Path) -> tuple[dict, list[dict]]:
    """Read the public packet, refusing one that no longer matches its hashes."""
    folder = Path(folder)
    rubric_doc = json.loads((folder / "rubric.json").read_text(encoding="utf-8"))
    rubric_hash = rubric_doc["rubric_sha256"]
    if digest(rubric_doc["rubric"]) != rubric_hash:
        raise PacketInvalid("rubric was changed after it was frozen")
    source = json.loads((folder / "samples.json").read_text(encoding="utf-8"))
    if source["rubric_sha256"] != rubric_hash:
        raise PacketInvalid("samples and rubric disagree about the rubric hash")
    samples, seen = [], set()
    for sample in source["samples"]:
        if digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]:
            raise PacketInvalid(f"{sample['sample_id']}: sample was changed after it was frozen")
        if sample["sample_id"] in seen:
            raise PacketInvalid(f"{sample['sample_id']}: duplicate sample")
        seen.add(sample["sample_id"])
        samples.append(sample)
    if not samples:
        raise PacketInvalid("the packet contains no samples")
    return rubric_doc, samples


def _lines(text: str) -> str:
    """The document with a gutter, so a citation names a line a reader can see.

    A trailing newline is not a line: ``samples/<id>.md`` stops at the last
    visible row, and a worksheet that numbered the empty string after it would
    show one row more than the document the reviewer was also handed. Sixteen of
    the twenty-four documents end with a newline, so this is the common case,
    not an edge case.
    """
    rows = text.split("\n")
    if len(rows) > 1 and not rows[-1].strip():
        rows.pop()
    rendered = []
    for number, line in enumerate(rows, start=1):
        shown = html.escape(line) if line.strip() else "&nbsp;"
        rendered.append(f'<div class="ln"><span class="n">{number}</span>'
                        f'<span class="t">{shown}</span></div>')
    return "".join(rendered)


def _rubric(rubric: dict) -> str:
    dimensions = "".join(f"<li><b>{html.escape(name)}</b> — {html.escape(text)}</li>"
                         for name, text in rubric["dimensions"].items())
    verdicts = "".join(f"<li><b>{html.escape(name)}</b> — {html.escape(text)}</li>"
                       for name, text in rubric["verdicts"].items())
    policy = rubric["calibration_policy"]
    return (f'<section class="rubric"><h2>Rubric {html.escape(rubric["version"])}</h2>'
            f'<h3>四个维度</h3><ul>{dimensions}</ul>'
            f'<h3>verdict 的含义</h3><ul>{verdicts}</ul>'
            f'<p class="muted">判定阈值（预登记）：accuracy ≥ {policy["accuracy_threshold"]}、'
            f'pass 召回 ≥ {policy["pass_recall_threshold"]}、fail 召回 ≥ '
            f'{policy["fail_recall_threshold"]}、每类至少 {policy["minimum_consensus_per_class"]} 条'
            f'共识。这些是校准门槛，不是本页要你算的分数。</p></section>')


def _card(sample: dict, rubric_hash: str) -> str:
    facts = "".join(f"<li>{html.escape(str(item))}</li>" for item in sample["facts"])
    sid = sample["sample_id"]
    return f'''<article class="card" id="{sid}" data-sample="{sid}"
  data-sample-sha="{sample['sample_sha256']}" data-rubric-sha="{rubric_hash}">
  <header><h2>{sid} <span class="tag">{html.escape(sample["dataset"])}</span></h2></header>
  <h3>Prompt</h3><p class="prompt">{html.escape(sample["prompt"])}</p>
  <h3>经提供的输入事实（判据只能依据这些）</h3><ul class="facts">{facts}</ul>
  <h3>文档（左侧为行号，evidence_lines 填这里的数字）</h3>
  <div class="doc">{_lines(sample["document"])}</div>
  <h3>最终回答</h3><pre class="answer">{html.escape(sample["final_answer"])}</pre>
  <div class="verdicts">
    <label><input type="radio" name="{sid}-verdict" value="pass"> pass</label>
    <label><input type="radio" name="{sid}-verdict" value="fail"> fail</label>
    <label><input type="radio" name="{sid}-verdict" value="inconclusive"> inconclusive</label>
  </div>
  <p><label class="field">evidence_lines（行号，逗号分隔；fail 必须指向有问题的行）<br>
    <input type="text" class="ev" placeholder="例如 7, 13"></label></p>
  <p><label class="field">note（依据；not_a_defect 类判断要写清检查了什么）<br>
    <textarea class="note" rows="2"></textarea></label></p>
</article>'''


PAGE = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>交接文档语义盲评 — reviewer __SLOT__</title>
<style>
:root { color-scheme: light dark; --fg:#1c1f23; --bg:#fbfbfa; --line:#e3e2df;
  --muted:#666b73; --card:#fff; --accent:#2f6f4f; }
@media (prefers-color-scheme: dark) { :root { --fg:#e8e6e1; --bg:#17191b;
  --line:#33373b; --muted:#9aa0a6; --card:#1f2225; --accent:#7fc4a0; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
  font:15px/1.6 -apple-system, "Segoe UI", "PingFang SC", sans-serif; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:16px; margin:0 0 8px; }
h3 { font-size:13px; text-transform:uppercase; letter-spacing:.04em;
  color:var(--muted); margin:16px 0 6px; }
.wrap { max-width:900px; margin:0 auto; }
.rubric, .card { background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:16px 18px; margin:16px 0; }
.tag { font-size:12px; font-weight:400; color:var(--muted); border:1px solid var(--line);
  border-radius:999px; padding:1px 8px; margin-left:6px; }
.muted { color:var(--muted); font-size:13px; }
.doc { font:13px/1.5 ui-monospace, "SF Mono", Menlo, monospace; border:1px solid var(--line);
  border-radius:8px; padding:8px 10px; max-height:420px; overflow:auto; }
.ln { display:flex; gap:10px; }
.ln .n { flex:0 0 34px; text-align:right; color:var(--muted); user-select:none;
  font-variant-numeric:tabular-nums; }
.ln .t { white-space:pre-wrap; word-break:break-word; flex:1; }
.answer { font:13px/1.55 ui-monospace, Menlo, monospace; white-space:pre-wrap;
  border:1px solid var(--line); border-radius:8px; padding:8px 10px; margin:0;
  max-height:280px; overflow:auto; }
.facts { margin:0; padding-left:22px; }
.verdicts { display:flex; gap:16px; margin:14px 0 6px; font-weight:600; }
.field { display:block; font-size:13px; color:var(--muted); }
input[type=text], textarea { width:100%; padding:7px 9px; margin-top:4px;
  border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg);
  font:inherit; }
.bar { position:sticky; top:0; z-index:5; background:var(--bg); padding:10px 0;
  display:flex; gap:12px; align-items:center; flex-wrap:wrap;
  border-bottom:1px solid var(--line); }
button { font:inherit; font-weight:600; padding:8px 14px; border-radius:8px;
  border:1px solid var(--accent); background:var(--accent); color:#fff; cursor:pointer; }
button.ghost { background:transparent; color:var(--accent); }
#status { font-size:13px; color:var(--muted); }
#export { width:100%; height:160px; margin-top:10px; display:none; }
.warn { color:#b4451f; font-weight:600; }
</style></head>
<body><div class="wrap">
<div class="bar">
  <strong>reviewer __SLOT__</strong>
  <label> reviewer_id <input type="text" id="who" placeholder="你的稳定名字，例如 reviewer-A" style="width:260px"></label>
  <button id="save" class="ghost">保存进度到本机</button>
  <button id="emit">导出 JSONL</button>
  <span id="status">__TOTAL__ 份待评，未开始</span>
</div>
<h1>交接文档语义盲评</h1>
<p class="muted">rubric sha256 <code>__RUBRIC__</code> · __TOTAL__ 份 ·
本页只含 public 材料，不含模型身份、槽位或自动判分。你的判断请独立作出，不要与另一位复核者交流。</p>
__RUBRIC_HTML__
<div id="cards">__CARDS__</div>
<p class="muted">导出后把 <code>reviewer-__SLOT__.jsonl</code> 交回，不修改
<code>reviewer-A.jsonl</code> / <code>reviewer-B.jsonl</code> 模板本身。</p>
<textarea id="export" readonly></textarea>
</div>
<script>
const SLOT = "__SLOT__", STORE = "chagent-review-" + SLOT + "-__RUBRIC_SHORT__";
const cards = [...document.querySelectorAll(".card")];
const status = document.getElementById("status");
const who = document.getElementById("who");

function read() {
  return cards.map(card => {
    const id = card.dataset.sample;
    const picked = card.querySelector('input[name="' + id + '-verdict"]:checked');
    const ev = card.querySelector(".ev").value.trim();
    return {
      sample_id: id,
      sample_sha256: card.dataset.sampleSha,
      rubric_sha256: card.dataset.rubricSha,
      reviewer_id: who.value.trim(),
      verdict: picked ? picked.value : null,
      evidence_lines: ev ? ev.split(/[,\\s]+/).filter(Boolean).map(Number).filter(n => n > 0
        && Number.isInteger(n)) : [],
      note: card.querySelector(".note").value.trim(),
    };
  });
}

function progress(rows) {
  const done = rows.filter(r => r.verdict).length;
  const need = rows.filter(r => r.verdict && (!r.reviewer_id || !r.evidence_lines.length || !r.note));
  status.innerHTML = done + "/" + rows.length + " 已判"
    + (need.length ? ' · <span class="warn">' + need.length + " 份缺 reviewer/行号/依据</span>" : "");
}

function snapshot() {
  const state = { who: who.value, rows: cards.map(card => ({
    verdict: (card.querySelector('input[name="' + card.dataset.sample + '-verdict"]:checked') || {}).value || null,
    ev: card.querySelector(".ev").value,
    note: card.querySelector(".note").value,
  })) };
  localStorage.setItem(STORE, JSON.stringify(state));
}

document.getElementById("save").onclick = () => { snapshot(); status.textContent = "已保存到本机浏览器"; };

document.getElementById("emit").onclick = () => {
  const rows = read();
  progress(rows);
  const box = document.getElementById("export");
  box.style.display = "block";
  box.value = rows.map(r => JSON.stringify(r)).join("\\n") + "\\n";
  box.select();
  const name = "reviewer-" + SLOT + ".jsonl";
  const url = URL.createObjectURL(new Blob([box.value], {type: "application/x-ndjson"}));
  const a = document.createElement("a"); a.href = url; a.download = name; a.click();
  URL.revokeObjectURL(url);
};

const saved = localStorage.getItem(STORE);
if (saved) {
  try {
    const state = JSON.parse(saved);
    who.value = state.who || "";
    cards.forEach((card, i) => {
      const row = state.rows[i]; if (!row) return;
      if (row.verdict) {
        const radio = card.querySelector('input[name="' + card.dataset.sample + '-verdict"][value="' + row.verdict + '"]');
        if (radio) radio.checked = true;
      }
      card.querySelector(".ev").value = row.ev || "";
      card.querySelector(".note").value = row.note || "";
    });
  } catch (e) {}
}
document.addEventListener("input", () => { snapshot(); progress(read()); });
progress(read());
</script>
</body></html>
'''


def build(folder: Path, slot: str, destination: Path | None = None) -> Path:
    """Render the worksheet for one reviewer slot. Labels stay empty."""
    if slot not in ("A", "B"):
        raise ValueError("slot must be A or B: two independent reviewers, no third file")
    rubric_doc, samples = load(folder)
    rubric_hash = rubric_doc["rubric_sha256"]
    page = (PAGE
            .replace("__SLOT__", slot)
            .replace("__TOTAL__", str(len(samples)))
            .replace("__RUBRIC_SHORT__", rubric_hash[:12])
            .replace("__RUBRIC__", rubric_hash)
            .replace("__RUBRIC_HTML__", _rubric(rubric_doc["rubric"]))
            .replace("__CARDS__", "".join(_card(sample, rubric_hash) for sample in samples)))
    target = Path(destination) if destination else Path(folder) / f"review-{slot}.html"
    target.write_text(page, encoding="utf-8")
    return target


def check(path: Path, folder: Path) -> list[str]:
    """Every reason a returned file cannot be used, or an empty list.

    The rules are the ones ``analyze_reviews.py`` enforces, reported before a
    reviewer is told their file was fine and a script later says otherwise.
    """
    rubric_doc, samples = load(folder)
    rubric_hash = rubric_doc["rubric_sha256"]
    known = {sample["sample_id"]: sample for sample in samples}
    problems, seen, reviewers, blank = [], set(), set(), []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            problems.append(f"line {number}: not JSON ({error.msg})")
            continue
        missing = [field for field in SAMPLE_FIELDS if field not in row]
        if missing:
            problems.append(f"line {number}: missing {', '.join(missing)}")
            continue
        sid = row["sample_id"]
        if sid not in known:
            problems.append(f"line {number}: unknown sample {sid}")
            continue
        if sid in seen:
            problems.append(f"line {number}: {sid} appears twice")
            continue
        seen.add(sid)
        if row["sample_sha256"] != known[sid]["sample_sha256"]:
            problems.append(f"line {number}: {sid} sample hash does not match the packet")
        if row["rubric_sha256"] != rubric_hash:
            problems.append(f"line {number}: {sid} rubric hash does not match the packet")
        verdict = row["verdict"]
        if verdict is None:
            blank.append(sid)
            continue
        if verdict not in VERDICTS:
            problems.append(f"line {number}: {sid} verdict {verdict!r} is not one of {VERDICTS}")
            continue
        reviewer = str(row["reviewer_id"] or "").strip()
        if reviewer:
            reviewers.add(reviewer)
        else:
            problems.append(f"line {number}: {sid} has a verdict but no reviewer_id")
        if not row["evidence_lines"]:
            problems.append(f"line {number}: {sid} has a verdict but no evidence_lines")
        elif not all(isinstance(item, int) and item > 0 for item in row["evidence_lines"]):
            problems.append(f"line {number}: {sid} evidence_lines must be positive line numbers")
        if not str(row["note"] or "").strip():
            problems.append(f"line {number}: {sid} has a verdict but no note")
    unseen = sorted(set(known) - seen)
    if unseen:
        problems.append(f"{len(unseen)} sample(s) absent: {', '.join(unseen[:6])}"
                        + (" …" if len(unseen) > 6 else ""))
    # A blank verdict is not a decision that happens to be missing: it is the
    # template, and a template returned as a review would be counted as a
    # reviewer who looked at nothing. Say so instead of passing it.
    if blank:
        problems.append(f"{len(blank)} sample(s) have no verdict: {', '.join(sorted(blank)[:6])}"
                        + (" …" if len(blank) > 6 else ""))
    if len(reviewers) > 1:
        problems.append("one file must contain exactly one reviewer, found: "
                        + ", ".join(sorted(reviewers)))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.review_sheet",
                                     description="Build and check the two-reviewer worksheet. "
                                                 "It never fills a verdict.")
    parser.add_argument("action", choices=("build", "check"))
    parser.add_argument("--packet", type=Path, required=True,
                        help="public/ directory of the blind packet")
    parser.add_argument("--slot", choices=("A", "B"), help="build: which reviewer this page is for")
    parser.add_argument("--out", type=Path, help="build: where to write the page")
    parser.add_argument("--file", type=Path, help="check: the returned JSONL")
    args = parser.parse_args(argv)

    try:
        if args.action == "build":
            if not args.slot:
                parser.error("build needs --slot A or --slot B")
            print(build(args.packet, args.slot, args.out))
            return 0
        if not args.file:
            parser.error("check needs --file")
        problems = check(args.file, args.packet)
    except PacketInvalid as error:
        print(f"packet rejected: {error}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(problem)
        return 1
    print("acceptable: every sample present, hashes match, one reviewer, no blank verdicts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
