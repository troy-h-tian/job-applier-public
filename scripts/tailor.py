"""Build a per-company tailored resume PDF from resume/resume.yaml.

The agent may only reword bullets (same facts, same numbers, no new tools or names), reorder
skills, and swap the Projects/Experience order. Headings, titles, dates and contact details always
come from resume.yaml, whose bullets must match resume/resume.md word for word. Anything that
fails these checks, or doesn't fit on one page, is rejected and the original resume/resume.pdf is
uploaded instead. Compiled with Tectonic (https://tectonic-typesetting.github.io).

  python3 scripts/tailor.py --check          # resume.yaml agrees with resume.md
  python3 scripts/tailor.py --base out.pdf   # render resume.yaml unchanged, to compare with resume.pdf
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import yaml

from common import DATA, ROOT

BASE = ROOT / "resume" / "resume.yaml"
RESUME_MD = ROOT / "resume" / "resume.md"
TEMPLATE = Path(__file__).resolve().parent / "resume_template.tex"
OUT_DIR = DATA / "tailored"

# Limits on how far a tailored bullet may drift from the original wording.
MAX_LENGTH_GROWTH = 1.08         # an entry's bullets may grow at most 8% (keeps it on one page)
MAX_NEW_WORDS_PER_BULLET = 5     # content words not found anywhere in the original resume
STOPWORDS = set("""a an and are as at be by for from has have in into is it its of on or that the
their this to was were which while with within across using via""".split())


def load_base():
    return yaml.safe_load(BASE.read_text())


# ---------- text helpers ----------

def plain(text):
    """Text without *italic* markers, whitespace collapsed."""
    return re.sub(r"\s+", " ", text.replace("*", "")).strip()


def tokens(text):
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9+#./&-]*[A-Za-z0-9+#]|[A-Za-z0-9]", plain(text))


def is_term(tok, sentence_start):
    """Names, tools and numbers: things a rewording must never introduce."""
    if re.search(r"[0-9+#]", tok) or re.search(r"[A-Z]", tok[1:]):
        return True
    return tok[0].isupper() and not sentence_start


def terms(text):
    out = []
    words = plain(text).split(" ")
    for i, w in enumerate(words):
        start = i == 0 or words[i - 1].endswith((".", ":", ";"))
        for t in tokens(w):
            if is_term(t, start):
                out.append(t)
    return out


def content_words(text):
    return [t.lower() for t in tokens(text) if t.isalpha() and len(t) >= 4 and t.lower() not in STOPWORDS]


def base_text(base):
    parts = [RESUME_MD.read_text()]
    for sec in base["sections"]:
        for e in sec["entries"]:
            parts += [e.get("title", ""), e.get("org", "")] + e["bullets"]
    return plain(" ".join(parts))


# ---------- checks ----------

def check_base(base=None):
    """resume.yaml must say exactly what resume.md says (resume.md is the ground truth)."""
    base = base or load_base()
    md = plain(re.sub(r"\*\*|`|^#+\s|^\s*-\s", " ", RESUME_MD.read_text(), flags=re.M))
    problems = []
    fields = [base["education"][k] for k in ("school", "degree", "dates")]
    for sec in base["sections"]:
        for e in sec["entries"]:
            fields += [e.get("title"), e.get("org"), e.get("role"), e.get("location"), e["dates"]] + e["bullets"]
    for s in base["skills"]:
        fields += s["items"]
    for f in fields:
        if f and plain(f) not in md:
            problems.append(f"resume.yaml text not found in resume.md: {plain(f)[:80]!r}")
    return problems


def validate(base, bullets, skills_order, section_order):
    problems = []
    entries = {e["id"]: e for sec in base["sections"] for e in sec["entries"]}
    vocab = {t.lower() for t in tokens(base_text(base))}
    for eid, new in (bullets or {}).items():
        if eid not in entries:
            problems.append(f"unknown entry id {eid!r}; valid ids: {', '.join(entries)}")
            continue
        old = entries[eid]["bullets"]
        if len(new) != len(old):
            problems.append(f"{eid}: give exactly {len(old)} bullets (got {len(new)})")
            continue
        old_text = " ".join(old)
        if len(plain(" ".join(new))) > MAX_LENGTH_GROWTH * len(plain(old_text)):
            problems.append(f"{eid}: bullets are too long; keep them within 8% of the original length")
        old_tokens = {t.lower() for t in tokens(old_text)}
        for b in new:
            for t in terms(b):
                if re.search(r"[0-9]", t) and t.lower() not in old_tokens:
                    problems.append(f"{eid}: number {t!r} is not in this entry's original bullets")
                elif t.lower() not in vocab:
                    problems.append(f"{eid}: {t!r} does not appear in the resume; don't add names, tools or terms")
            new_words = sorted({w for w in content_words(b) if w not in vocab})
            if len(new_words) > MAX_NEW_WORDS_PER_BULLET:
                problems.append(f"{eid}: too many words not in the original resume ({', '.join(new_words)}); "
                                "reword more lightly")
    for s in base["skills"]:
        order = (skills_order or {}).get(s["label"])
        if order is not None and Counter(order) != Counter(s["items"]):
            problems.append(f"skills {s['label']!r}: must be a reordering of exactly: {', '.join(s['items'])}")
    unknown = set(skills_order or {}) - {s["label"] for s in base["skills"]}
    if unknown:
        problems.append(f"unknown skills labels {sorted(unknown)}; valid: {[s['label'] for s in base['skills']]}")
    if section_order and sorted(section_order) != sorted(s["id"] for s in base["sections"]):
        problems.append(f"section_order must be a reordering of {[s['id'] for s in base['sections']]}")
    return problems


# ---------- rendering ----------

def tex(text):
    """Escape for LaTeX; *text* becomes italics."""
    def esc(s):
        s = s.replace("\\", "\\textbackslash{}")
        s = re.sub(r"([&%$#_{}])", r"\\\1", s)
        return s.replace("~", "\\textasciitilde{}").replace("^", "\\textasciicircum{}")
    parts = re.split(r"\*([^*]+)\*", plain_spaces(text))
    return "".join(esc(p) if i % 2 == 0 else "\\textit{" + esc(p) + "}" for i, p in enumerate(parts))


def plain_spaces(text):
    return re.sub(r"\s+", " ", text).strip()


def render(data):
    lines = ["\\begin{center}", f"  \\textbf{{\\Huge {tex(data['name'])}}} \\\\ \\vspace{{1pt}}", "  \\small "]
    contact = []
    for c in data["contact"]:
        contact.append(f"\\href{{{c['url']}}}{{\\underline{{{tex(c['text'])}}}}}" if c.get("url") else tex(c["text"]))
    lines[-1] += " $|$ ".join(contact)
    lines += ["\\end{center}", ""]

    ed = data["education"]
    lines += ["\\section{Education}", "  \\resumeSubHeadingListStart",
              f"    \\resumeSubheading{{{tex(ed['school'])}}}{{{tex(ed['right'])}}}"
              f"{{{tex(ed['degree'])}}}{{{tex(ed['dates'])}}}",
              "  \\resumeSubHeadingListEnd", ""]

    for sec in data["sections"]:
        lines += [f"\\section{{{tex(sec['title'])}}}", "  \\resumeSubHeadingListStart"]
        for e in sec["entries"]:
            if "org" in e:
                lines.append(f"    \\resumeSubheading{{{tex(e['org'])}}}{{{tex(e['dates'])}}}"
                             f"{{{tex(e['role'])}}}{{{tex(e['location'])}}}")
            else:
                lines.append(f"    \\resumeProjectHeading{{\\textbf{{{tex(e['title'])}}}}}{{{tex(e['dates'])}}}")
            lines.append("      \\resumeItemListStart")
            lines += [f"        \\resumeItem{{{tex(b)}}}" for b in e["bullets"]]
            lines.append("      \\resumeItemListEnd")
        lines += ["  \\resumeSubHeadingListEnd", ""]

    lines += ["\\section{Technical Skills}", " \\begin{itemize}[leftmargin=0.15in, label={}]",
              "    \\small{\\item{"]
    lines.append(" \\\\\n".join(f"     \\textbf{{{tex(s['label'])}}}: {tex(', '.join(s['items']))}" for s in data["skills"]))
    lines += ["    }}", " \\end{itemize}", "", "\\AtEndDocument{\\message{TAILOR-PAGES=\\thepage}}"]
    return TEMPLATE.read_text().replace("%%BODY%%", "\n".join(lines))


def compile_pdf(source, out_pdf):
    """Compile LaTeX source to out_pdf; returns the page count."""
    exe = os.environ.get("TECTONIC") or shutil.which("tectonic")
    if not exe:
        raise RuntimeError("tectonic is not installed, so tailored resumes are unavailable")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.tex"
        src.write_text(source)
        r = subprocess.run([exe, "--keep-logs", "--chatter", "minimal", "--outdir", tmp, str(src)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("LaTeX compile failed: " + (r.stderr or r.stdout)[-800:])
        log = (Path(tmp) / "resume.log").read_text(errors="replace")
        m = re.search(r"TAILOR-PAGES=(\d+)", log)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path(tmp) / "resume.pdf", out_pdf)
    return int(m.group(1)) if m else 0


def pdf_name(base):
    first, *_, last = base["name"].split()
    return f"{last}_{first}.pdf"


def tailor(job_id, bullets=None, skills_order=None, section_order=None, reason=""):
    """Validate the requested changes and build the PDF. Returns its path; raises ValueError."""
    base = load_base()
    problems = check_base(base) + validate(base, bullets, skills_order, section_order)
    if problems:
        raise ValueError("\n".join(problems))
    data = json.loads(json.dumps(base))
    for sec in data["sections"]:
        for e in sec["entries"]:
            if e["id"] in (bullets or {}):
                e["bullets"] = bullets[e["id"]]
    for s in data["skills"]:
        s["items"] = (skills_order or {}).get(s["label"], s["items"])
    if section_order:
        by_id = {s["id"]: s for s in data["sections"]}
        data["sections"] = [by_id[i] for i in section_order]

    out = OUT_DIR / re.sub(r"[^A-Za-z0-9._-]+", "_", job_id) / pdf_name(base)
    pages = compile_pdf(render(data), out)
    if pages != 1:
        out.unlink(missing_ok=True)
        raise ValueError(f"the tailored resume runs to {pages} pages; shorten the rewritten bullets")
    (out.parent / "changes.json").write_text(json.dumps(
        {"job_id": job_id, "reason": reason, "bullets": bullets or {}, "skills_order": skills_order or {},
         "section_order": section_order or []}, indent=2, ensure_ascii=False) + "\n")
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="check resume.yaml against resume.md")
    p.add_argument("--base", metavar="OUT_PDF", help="render resume.yaml unchanged to this path")
    a = p.parse_args()
    if a.check:
        problems = check_base()
        print("\n".join(problems) or "resume.yaml matches resume.md")
        raise SystemExit(1 if problems else 0)
    if a.base:
        print(f"{compile_pdf(render(load_base()), Path(a.base))} page(s) -> {a.base}")


if __name__ == "__main__":
    main()
