#!/usr/bin/env python3
"""
coursera_dl.py — Coursera course downloader (videos + subtitles + readings + labs)

Downloads content from a Coursera course you are ENROLLED IN, for personal
offline study, using your own browser session cookie (CAUTH).

What it grabs, per course item:
  - lecture        -> highest-quality MP4 (via yt-dlp) + subtitles (.vtt)
  - supplement     -> the reading, saved as a clean .html file (+ any attached files)
  - ungradedLti    -> the LAB: saves the instructions and downloads the linked
                      lab notebook(s)/files (e.g. IBM Skills Network .ipynb on their
                      public CDN). See README for why labs are only partially grabbable.

It deliberately SKIPS quizzes/graded assignments (there is nothing to "download"
there — they are interactive).

Auth: pass your CAUTH cookie with --cauth '<value>' or --cauth-file cauth.txt.
See the README for how to copy CAUTH from your browser.

Requires: Python 3.8+, `requests` (pip install requests), and `yt-dlp` on PATH
(pip install yt-dlp) for video downloads. Use --downloader requests to avoid yt-dlp.

Personal/fair use only. You are responsible for complying with Coursera's Terms
of Use and the content licenses of the courses you are enrolled in.
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote, urljoin, unquote

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: requests.  Install with:  pip install requests")

BASE = "https://www.coursera.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Item content types we know how to handle. Everything else is skipped.
DOWNLOADABLE = {"lecture", "supplement", "ungradedLti"}

# File extensions we are willing to pull from a lab's instruction links.
LAB_FILE_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:ipynb|zip|pdf|csv|json|py|txt|tar|gz|xlsx?|pptx?|docx?|md|sql|sh)",
    re.IGNORECASE,
)
# Hosts that commonly serve IBM Skills Network / lab assets (download even w/o a known ext).
LAB_HOST_RE = re.compile(r"(?:labs\.skills\.network|cf-courses-data|cognitiveclass|cocl\.us)", re.I)
ANY_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg, *, indent=0):
    print(("  " * indent) + msg, flush=True)


def sanitize(name, maxlen=120):
    """Make a string safe to use as a file/folder name."""
    name = html.unescape(name or "").strip()
    name = re.sub(r"[\\/:*?\"<>|\t\n\r]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
    return name or "untitled"


class Coursera:
    def __init__(self, cauth, quality="best", sub_langs=("en",), downloader="yt-dlp",
                 skip_existing=True, timeout=60):
        self.quality = quality
        self.sub_langs = list(sub_langs)
        self.downloader = downloader
        self.skip_existing = skip_existing
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self.s.cookies.set("CAUTH", cauth, domain="www.coursera.org")
        self.user_id = None

    # ---- low-level API ---- #
    def _get(self, url, *, as_json=True, tries=4):
        last = None
        for attempt in range(tries):
            try:
                r = self.s.get(url, timeout=self.timeout)
                if r.status_code == 401:
                    raise SystemExit("401 Unauthorized — your CAUTH cookie is wrong or "
                                     "expired. Copy a fresh one from your browser.")
                if r.status_code == 403:
                    last = f"403 Forbidden on {url}"
                    # often transient / locked item; back off and retry once or twice
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json() if as_json else r
            except requests.RequestException as e:
                last = str(e)
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Request failed after {tries} tries: {last}")

    def resolve_user(self):
        data = self._get(f"{BASE}/api/adminUserPermissions.v1?q=my")
        self.user_id = data["elements"][0]["id"]
        return self.user_id

    def resolve_course_id(self, slug):
        data = self._get(f"{BASE}/api/onDemandCourses.v1?q=slug&slug={quote(slug)}")
        els = data.get("elements") or []
        if not els:
            raise RuntimeError(f"Course not found for slug '{slug}'. Check the slug.")
        return els[0]["id"], els[0].get("name", slug)

    def resolve_specialization(self, slug):
        """Expand a Specialization / Professional Certificate slug into its ordered
        member courses. Returns (spec_name, [(course_slug, course_name), ...])."""
        fields = "name,slug,courseIds"
        url = (f"{BASE}/api/onDemandSpecializations.v1?q=slug&slug={quote(slug)}"
               f"&fields={quote(fields, safe=',')}&includes=courses")
        data = self._get(url)
        els = data.get("elements") or []
        if not els:
            raise RuntimeError(
                f"Specialization/Certificate not found for slug '{slug}'. "
                f"Use the slug after /specializations/ or /professional-certificates/.")
        spec = els[0]
        courses = data.get("linked", {}).get("courses.v1", [])
        by_id = {c["id"]: (c.get("slug"), c.get("name", c.get("slug")))
                 for c in courses}
        ordered = []
        for cid in spec.get("courseIds", []):
            if cid in by_id and by_id[cid][0]:
                ordered.append(by_id[cid])
        if not ordered:  # fallback: whatever came back in linked
            ordered = [(c.get("slug"), c.get("name")) for c in courses if c.get("slug")]
        return spec.get("name", slug), ordered

    def get_tree(self, slug):
        """Return ordered list of (module, lesson, item) dicts."""
        fields = (
            "onDemandCourseMaterialModules.v1(name,slug,lessonIds),"
            "onDemandCourseMaterialLessons.v1(name,slug,itemIds),"
            "onDemandCourseMaterialItems.v2(name,slug,contentSummary,isLocked,itemLockedReasonCode)"
        )
        url = (f"{BASE}/api/onDemandCourseMaterials.v2/?q=slug&slug={quote(slug)}"
               f"&includes=modules,lessons,items&fields={quote(fields, safe='(),.')}"
               f"&showLockedItems=true")
        data = self._get(url)
        linked = data.get("linked", {})
        modules = {m["id"]: m for m in linked.get("onDemandCourseMaterialModules.v1", [])}
        lessons = {l["id"]: l for l in linked.get("onDemandCourseMaterialLessons.v1", [])}
        items = {i["id"]: i for i in linked.get("onDemandCourseMaterialItems.v2", [])}

        # Preserve order: course root -> moduleIds, module -> lessonIds, lesson -> itemIds
        root = (data.get("elements") or [{}])[0]
        module_order = root.get("moduleIds") or list(modules.keys())

        tree = []
        for mi, mid in enumerate(module_order, 1):
            m = modules.get(mid)
            if not m:
                continue
            for li, lid in enumerate(m.get("lessonIds", []), 1):
                l = lessons.get(lid)
                if not l:
                    continue
                for ii, iid in enumerate(l.get("itemIds", []), 1):
                    it = items.get(iid)
                    if not it:
                        continue
                    tree.append({"m_idx": mi, "l_idx": li, "i_idx": ii,
                                 "module": m, "lesson": l, "item": it})
        return tree

    # ---- per-item content ---- #
    def video_sources(self, course_id, item_id):
        fields = "onDemandVideos.v1(sources,subtitles,subtitlesVtt)"
        url = (f"{BASE}/api/onDemandLectureVideos.v1/{course_id}~{item_id}"
               f"?includes=video&fields={quote(fields, safe='(),.')}")
        data = self._get(url)
        vids = data.get("linked", {}).get("onDemandVideos.v1", [])
        if not vids:
            return None
        v = vids[0]
        by_res = (v.get("sources") or {}).get("byResolution") or {}
        # subtitlesVtt preferred, fall back to subtitles (srt)
        subs = v.get("subtitlesVtt") or v.get("subtitles") or {}
        return {"byResolution": by_res, "subs": subs,
                "is_vtt": bool(v.get("subtitlesVtt"))}

    def pick_resolution(self, by_res):
        if not by_res:
            return None, None
        # resolutions look like "1080p","720p",...; sort numerically desc
        def h(k):
            m = re.match(r"(\d+)", k)
            return int(m.group(1)) if m else 0
        res_sorted = sorted(by_res.keys(), key=h, reverse=True)
        if self.quality != "best":
            want = self.quality.rstrip("p")
            for r in res_sorted:
                if r.rstrip("p") == want:
                    return r, by_res[r].get("mp4VideoUrl")
            log(f"requested quality {self.quality} not available; using {res_sorted[0]}", indent=3)
        chosen = res_sorted[0]
        return chosen, by_res[chosen].get("mp4VideoUrl")

    def supplement_html(self, course_id, item_id):
        fields = "openCourseAssets.v1(typeName,definition)"
        url = (f"{BASE}/api/onDemandSupplements.v1/{course_id}~{item_id}"
               f"?includes=asset&fields={quote(fields, safe='(),.')}")
        data = self._get(url)
        assets = data.get("linked", {}).get("openCourseAssets.v1", [])
        htmls, cmls = [], []
        for a in assets:
            d = a.get("definition", {}) or {}
            rh = (d.get("renderableHtmlWithMetadata") or {}).get("renderableHtml")
            if rh:
                htmls.append(rh)
            if d.get("value"):
                cmls.append(d["value"])
        return "\n<hr>\n".join(htmls), "\n".join(cmls)

    def lab_description(self, course_id, item_id):
        fields = "ltiVersion,privacyLevel,toolName,description"
        url = (f"{BASE}/api/onDemandLtiDescriptions.v1/"
               f"{self.user_id}~{course_id}~{item_id}?fields={quote(fields, safe=',')}")
        try:
            data = self._get(url)
        except Exception as e:
            log(f"could not load lab description: {e}", indent=3)
            return "", ""
        el = (data.get("elements") or [{}])[0]
        desc = el.get("description")
        if isinstance(desc, dict):
            value = (desc.get("definition") or {}).get("value", "")
        else:
            value = desc or ""
        return el.get("toolName", ""), value

    def resolve_asset(self, asset_id):
        """Resolve a Coursera asset id -> (filename, download_url)."""
        url = (f"{BASE}/api/assets.v1/{asset_id}"
               f"?fields=name,typeName,url,fileExtension")
        try:
            data = self._get(url)
        except Exception:
            return None
        el = (data.get("elements") or [{}])[0]
        dl = (el.get("url") or {}).get("url")
        if not dl:
            return None
        name = el.get("name") or asset_id
        ext = el.get("fileExtension")
        if ext and not name.lower().endswith("." + ext.lower()):
            name = f"{name}.{ext}"
        return name, dl

    # ---- downloads ---- #
    def download_file(self, url, dest):
        if self.skip_existing and os.path.exists(dest) and os.path.getsize(dest) > 0:
            log(f"skip (exists): {os.path.basename(dest)}", indent=3)
            return True
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            with self.s.get(url, stream=True, timeout=self.timeout) as r:
                r.raise_for_status()
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, dest)
            log(f"saved: {os.path.basename(dest)}", indent=3)
            return True
        except Exception as e:
            log(f"FAILED {os.path.basename(dest)}: {e}", indent=3)
            return False

    def download_video(self, url, dest):
        if self.skip_existing and os.path.exists(dest) and os.path.getsize(dest) > 0:
            log(f"skip (exists): {os.path.basename(dest)}", indent=3)
            return True
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if self.downloader == "yt-dlp" and shutil.which("yt-dlp"):
            cmd = ["yt-dlp", "--no-mtime", "--continue", "--retries", "10",
                   "--fragment-retries", "10", "--no-warnings", "--quiet",
                   "--progress", "-o", dest, url]
            try:
                subprocess.run(cmd, check=True)
                log(f"saved (yt-dlp): {os.path.basename(dest)}", indent=3)
                return True
            except subprocess.CalledProcessError as e:
                log(f"yt-dlp failed ({e}); falling back to direct download", indent=3)
        return self.download_file(url, dest)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def wrap_html(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title>"
            f"<style>body{{font-family:system-ui,Arial,sans-serif;max-width:820px;"
            f"margin:2rem auto;padding:0 1rem;line-height:1.5}}img{{max-width:100%}}"
            f"pre,code{{background:#f4f4f4;padding:.1rem .3rem;border-radius:4px}}</style>"
            f"</head><body><h1>{html.escape(title)}</h1>\n{body}\n</body></html>")


def extract_asset_ids(cml, html_text):
    ids = set(re.findall(r'<asset[^>]*\bid="([^"]+)"', cml or ""))
    ids |= set(re.findall(r'data-asset-id="([^"]+)"', html_text or ""))
    return ids


def lab_links(text):
    """Return downloadable URLs referenced in a lab's instruction CML/HTML."""
    found = set()
    for u in ANY_URL_RE.findall(text or ""):
        u = html.unescape(u).rstrip(').,"\'')
        if LAB_FILE_RE.match(u) or LAB_HOST_RE.search(u):
            found.add(u)
    return sorted(found)


def process_course(cr, slug, out_root, include, course_prefix=None):
    course_id, course_name = cr.resolve_course_id(slug)
    folder = (f"{course_prefix} - {sanitize(course_name)}" if course_prefix
              else sanitize(course_name))
    course_dir = os.path.join(out_root, folder)
    os.makedirs(course_dir, exist_ok=True)
    log(f"\n=== {('['+course_prefix+'] ') if course_prefix else ''}{course_name}  ({slug}) ===")
    tree = cr.get_tree(slug)
    log(f"{len(tree)} items across the course. Output -> {course_dir}")

    counts = {"lecture": 0, "supplement": 0, "ungradedLti": 0, "skipped": 0, "locked": 0}

    for node in tree:
        it = node["item"]
        ctype = (it.get("contentSummary") or {}).get("typeName")
        name = it.get("name", "item")
        prefix = f"{node['m_idx']:02d}.{node['l_idx']:02d}.{node['i_idx']:02d}"
        module_dir = os.path.join(course_dir,
                                  f"{node['m_idx']:02d} - {sanitize(node['module'].get('name',''))}")

        if ctype not in DOWNLOADABLE:
            counts["skipped"] += 1
            continue
        if ctype not in include:
            continue
        if it.get("isLocked"):
            log(f"[LOCKED] {prefix} {name} ({it.get('itemLockedReasonCode','')})", indent=1)
            counts["locked"] += 1
            continue

        base = f"{prefix} {sanitize(name)}"
        log(f"[{ctype}] {prefix} {name}", indent=1)

        try:
            if ctype == "lecture":
                info = cr.video_sources(course_id, it["id"])
                if not info:
                    log("no video sources", indent=3); continue
                res, mp4 = cr.pick_resolution(info["byResolution"])
                if mp4:
                    cr.download_video(mp4, os.path.join(module_dir, base + f" [{res}].mp4"))
                # subtitles
                subs = info["subs"] or {}
                want = subs.keys() if "all" in cr.sub_langs else \
                    [l for l in cr.sub_langs if l in subs]
                ext = "vtt" if info["is_vtt"] else "srt"
                for lang in want:
                    suburl = subs[lang]
                    if suburl.startswith("/"):
                        suburl = urljoin(BASE, suburl)
                    cr.download_file(suburl,
                                     os.path.join(module_dir, f"{base}.{lang}.{ext}"))
                counts["lecture"] += 1

            elif ctype == "supplement":
                body, cml = cr.supplement_html(course_id, it["id"])
                if body:
                    with open(os.path.join(module_dir, base + ".html"), "w",
                              encoding="utf-8") as f:
                        f.write(wrap_html(name, body))
                    log(f"saved: {base}.html", indent=3)
                # attached downloadable files inside the reading
                for aid in extract_asset_ids(cml, body):
                    resolved = cr.resolve_asset(aid)
                    if resolved:
                        fname, durl = resolved
                        cr.download_file(durl, os.path.join(module_dir, sanitize(fname)))
                counts["supplement"] += 1

            elif ctype == "ungradedLti":   # LAB
                tool, value = cr.lab_description(course_id, it["id"])
                lab_dir = os.path.join(module_dir, base + " [LAB]")
                os.makedirs(lab_dir, exist_ok=True)
                if value:
                    with open(os.path.join(lab_dir, "lab_instructions.html"), "w",
                              encoding="utf-8") as f:
                        f.write(wrap_html(name, value))
                    log("saved: lab_instructions.html", indent=3)
                links = lab_links(value)
                if not links:
                    log("no downloadable lab files found in the launch page "
                        "(lab likely runs only inside the external environment)", indent=3)
                for u in links:
                    fname = sanitize(unquote(os.path.basename(u.split("?")[0]))) or "labfile"
                    cr.download_file(u, os.path.join(lab_dir, fname))
                counts["ungradedLti"] += 1

        except Exception as e:
            log(f"ERROR on '{name}': {e}", indent=2)

    log(f"\nDone: {counts['lecture']} videos, {counts['supplement']} readings, "
        f"{counts['ungradedLti']} labs | {counts['locked']} locked, "
        f"{counts['skipped']} non-downloadable items skipped.")


def process_specialization(cr, slug, out_root, include, do_list=False):
    spec_name, courses = cr.resolve_specialization(slug)
    spec_dir = os.path.join(out_root, sanitize(spec_name))
    log(f"\n########## Specialization: {spec_name} "
        f"({len(courses)} courses) ##########")
    if do_list:
        for i, (cslug, cname) in enumerate(courses, 1):
            log(f"  {i:02d}. {cname}  ({cslug})")
        return
    os.makedirs(spec_dir, exist_ok=True)
    for i, (cslug, cname) in enumerate(courses, 1):
        try:
            process_course(cr, cslug, spec_dir, include, course_prefix=f"{i:02d}")
        except SystemExit:
            raise
        except Exception as e:
            log(f"!! Course {i:02d} '{cslug}' failed: {e}")


def read_cauth(args):
    if args.cauth:
        return args.cauth.strip()
    if args.cauth_file:
        with open(args.cauth_file, encoding="utf-8") as f:
            return f.read().strip()
    env = os.environ.get("COURSERA_CAUTH")
    if env:
        return env.strip()
    sys.exit("No CAUTH provided. Use --cauth '<value>', --cauth-file cauth.txt, "
             "or set COURSERA_CAUTH. See the README for how to get it.")


def main():
    p = argparse.ArgumentParser(
        description="Download Coursera course videos, subtitles, readings and labs "
                    "(personal offline use; you must be enrolled).")
    p.add_argument("slugs", nargs="*",
                   help="Course slug(s). The slug is the part after /learn/ in the URL, "
                        "e.g. develop-generative-ai-applications-get-started")
    p.add_argument("-s", "--specialization", nargs="+", default=[], metavar="SLUG",
                   help="Specialization or Professional Certificate slug(s) — the part "
                        "after /specializations/ or /professional-certificates/. Each is "
                        "expanded into all its member courses, saved in a specialization "
                        "folder with each course numbered 01, 02, ...")
    p.add_argument("--cauth", help="Your CAUTH cookie value")
    p.add_argument("--cauth-file", help="File containing only the CAUTH value")
    p.add_argument("--out", default="./coursera", help="Output directory (default ./coursera)")
    p.add_argument("--quality", default="best",
                   help="Video quality: best (default), or 1080p/720p/540p/360p/240p")
    p.add_argument("--subtitles", default="en",
                   help="Comma-separated subtitle languages, or 'all', or 'none' (default en)")
    p.add_argument("--include", default="video,reading,lab",
                   help="What to download: any of video,reading,lab (comma-separated). "
                        "Default: video,reading,lab")
    p.add_argument("--downloader", choices=["yt-dlp", "requests"], default="yt-dlp",
                   help="Video downloader (default yt-dlp; falls back to requests if absent)")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-download even if the file already exists")
    p.add_argument("--list", action="store_true",
                   help="Only list the course structure, download nothing")
    args = p.parse_args()

    if not args.slugs and not args.specialization:
        p.error("provide at least one course slug, or -s/--specialization <slug>.")

    # map friendly include names -> content types
    inc_map = {"video": "lecture", "reading": "supplement", "lab": "ungradedLti"}
    include = {inc_map[x.strip()] for x in args.include.split(",") if x.strip() in inc_map}
    if not include:
        sys.exit("--include must contain at least one of: video, reading, lab")

    sub_langs = [] if args.subtitles.strip().lower() == "none" \
        else [s.strip() for s in args.subtitles.split(",") if s.strip()]

    cauth = read_cauth(args)
    cr = Coursera(cauth, quality=args.quality, sub_langs=sub_langs,
                  downloader=args.downloader, skip_existing=not args.overwrite)

    log("Authenticating...")
    cr.resolve_user()
    log(f"OK (user id {cr.user_id}).")

    if args.list:
        for spec in args.specialization:
            process_specialization(cr, spec, args.out, include, do_list=True)
        for slug in args.slugs:
            cid, cname = cr.resolve_course_id(slug)
            log(f"\n=== {cname} ({slug}) ===")
            for n in cr.get_tree(slug):
                ct = (n["item"].get("contentSummary") or {}).get("typeName")
                mark = {"lecture": "📹", "supplement": "📄",
                        "ungradedLti": "🧪"}.get(ct, "·")
                lock = " [LOCKED]" if n["item"].get("isLocked") else ""
                log(f"{n['m_idx']:02d}.{n['l_idx']:02d}.{n['i_idx']:02d} "
                    f"{mark} {n['item'].get('name')}  ({ct}){lock}")
        return

    for spec in args.specialization:
        try:
            process_specialization(cr, spec, args.out, include)
        except SystemExit:
            raise
        except Exception as e:
            log(f"!! Specialization '{spec}' failed: {e}")

    for slug in args.slugs:
        try:
            process_course(cr, slug, args.out, include)
        except SystemExit:
            raise
        except Exception as e:
            log(f"!! Course '{slug}' failed: {e}")

    log("\nAll done.")


if __name__ == "__main__":
    main()
