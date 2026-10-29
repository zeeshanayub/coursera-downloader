# Coursera Downloader (`coursera_dl.py`)

Downloads **videos + subtitles + readings + labs** from a Coursera course you are
**enrolled in**, for personal offline study. Built and verified against the live
Coursera API using the IBM course *Develop Generative AI Applications: Get Started*.

It uses your browser session cookie (`CAUTH`) for auth, walks Coursera's internal
course-material API to find every item, and downloads:

| Coursera item type | What you get |
|---|---|
| **Lecture** (video) | Highest-quality **MP4** (via `yt-dlp`) + subtitles as `.vtt` |
| **Supplement** (reading) | The reading saved as a clean **`.html`** file, plus any attached files |
| **Lab** (`ungradedLti`) | Lab **instructions** + the linked **notebook(s)/files** (e.g. IBM Skills Network `.ipynb`) |

Quizzes and graded assignments are skipped on purpose — they're interactive, there's
nothing to "download."

> ⚠️ **Personal / fair use only.** Use this only for courses you're legitimately
> enrolled in, for your own offline study. You're responsible for complying with
> Coursera's Terms of Use and each course's content license. Don't redistribute.

---

## 1. Install

Requires **Python 3.11 or newer** (`yt-dlp` has dropped 3.10 and below).

```bash
git clone https://github.com/<your-username>/coursera-downloader.git
cd coursera-downloader

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

`yt-dlp` is used for the video downloads (resumable, with progress). If it's not on
your PATH the script automatically falls back to a plain streaming download, or you
can force that with `--downloader requests`.

---

## 2. Get your CAUTH cookie

`CAUTH` is the session cookie that proves you're logged in. It's `httpOnly`, so you
copy it from DevTools, not from JavaScript:

1. Log in to Coursera in your browser.
2. Open **DevTools** (`F12`) → **Application** tab (Chrome) or **Storage** (Firefox).
3. Left sidebar → **Cookies** → `https://www.coursera.org`.
4. Find the row named **`CAUTH`** and copy its **Value** (it's a long string).

Then either pass it inline, put it in a file, or set an env var:

```bash
# inline
python3 coursera_dl.py <slug> --cauth 'PASTE_THE_LONG_VALUE'

# from a file (recommended — keeps it out of your shell history)
echo 'PASTE_THE_LONG_VALUE' > cauth.txt
python3 coursera_dl.py <slug> --cauth-file cauth.txt

# or env var
export COURSERA_CAUTH='PASTE_THE_LONG_VALUE'
python3 coursera_dl.py <slug>
```

The cookie expires periodically (and when you log out). If you get a `401`, grab a
fresh one.

---

## 3. The course **slug**

The slug is the part of the URL right after `/learn/`:

```
https://www.coursera.org/learn/develop-generative-ai-applications-get-started/home/welcome
                                └──────────────── slug ──────────────────────┘
```

---

## 4. Usage

```bash
# See what's in the course first (downloads nothing)
python3 coursera_dl.py develop-generative-ai-applications-get-started --cauth-file cauth.txt --list

# Download everything (videos + readings + labs) into ./coursera
python3 coursera_dl.py develop-generative-ai-applications-get-started --cauth-file cauth.txt

# Only the labs (notebooks + instructions)
python3 coursera_dl.py <slug> --cauth-file cauth.txt --include lab

# Videos only, 720p, English + Spanish subtitles
python3 coursera_dl.py <slug> --cauth-file cauth.txt --include video --quality 720p --subtitles en,es

# Several courses at once, custom output folder
python3 coursera_dl.py slug-one slug-two --cauth-file cauth.txt --out ~/Coursera
```

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--cauth` / `--cauth-file` | — | Your CAUTH cookie (required; or `COURSERA_CAUTH` env) |
| `--out` | `./coursera` | Output directory |
| `--quality` | `best` | `best`, or `1080p` / `720p` / `540p` / `360p` / `240p` |
| `--subtitles` | `en` | Comma list of langs (e.g. `en,fr`), or `all`, or `none` |
| `--include` | `video,reading,lab` | Any combination of `video`, `reading`, `lab` |
| `--downloader` | `yt-dlp` | `yt-dlp` or `requests` |
| `--overwrite` | off | Re-download files that already exist |
| `--list` | off | Print the course tree only |

### Output layout

```
coursera/
└── Develop Generative AI Applications Get Started/
    ├── 01 - Foundations of Generative AI and Prompt Engineering/
    │   ├── 01.01.01 Course Introduction [1080p].mp4
    │   ├── 01.01.01 Course Introduction.en.vtt
    │   ├── 01.02.03 Course Overview.html
    │   └── 01.02.05 Lab Master Prompt Engineering... [LAB]/
    │       ├── lab_instructions.html
    │       └── In-Context Learning and Prompt Templates-v3-GenAIcourse.ipynb
    └── 02 - Introduction to LangChain in GenAI Applications/
        └── ...
```

Files are numbered `module.lesson.item` so they stay in course order. Re-running
skips what's already downloaded, so it's safe to resume.

---

## 5. About labs — read this

This is the part people get wrong, so here's the honest version based on how these
labs actually work.

Coursera labs are **external tools** (LTI items). When you click a lab, Coursera
doesn't serve the lab itself — it hands you off to a separate hosted environment
(for IBM courses, **IBM Skills Network Labs / SN Labs**), passing your name and
email. The actual running notebook lives in that container, **not** on Coursera, so
**there is no Coursera API that returns the live lab environment**. No tool can
"download the lab" in the sense of the full running sandbox.

**What this script *can* get**, and does:

- The **lab instructions** (saved as `lab_instructions.html`).
- Any **notebook or data files linked from the lab's launch page**. For IBM Skills
  Network labs, the launch page includes a "download or print the lab instructions"
  link that points straight at the lab's **`.ipynb` notebook on a public CDN**
  (`cf-courses-data.static.labs.skills.network/...`). That's the real lab notebook,
  and the script downloads it. It also catches linked `.zip`, `.pdf`, `.csv`, `.py`
  files on the lab hosts.

**What it can't get automatically:**

- Files that only exist *inside* the running lab container (datasets the environment
  generates, files you create during the lab, anything not linked on the launch page).
  For those, open the lab, and in its Jupyter/IDE use **File → Download**, or zip the
  working directory in a terminal cell and download that.
- Many IBM labs also publish their notebooks on GitHub
  (`github.com/ibm-developer-skills-network`) — worth a look if a notebook isn't
  linked on the launch page.

If the script prints *"no downloadable lab files found"* for a lab, that lab simply
didn't expose a file on its launch page — open it and use the in-lab download.

---

## 6. Troubleshooting

- **`401 Unauthorized`** — CAUTH is wrong or expired. Copy a fresh one.
- **`403` on some items** — usually a locked item (not yet unlocked on your schedule)
  or a transient rate-limit; the script retries and reports locked items.
- **A video has no MP4** — rare; some items are HLS-only. Re-run with
  `--downloader yt-dlp` (it handles more stream types than the plain fallback).
- **Slow / throttled** — Coursera rate-limits. Just let it run; re-runs resume.

---

## 7. How it works (for the curious)

All endpoints are Coursera's own internal `on-demand` API, called with your `CAUTH`
cookie:

1. `adminUserPermissions.v1?q=my` → your user id
2. `onDemandCourses.v1?q=slug&slug=<slug>` → course id
3. `onDemandCourseMaterials.v2` → modules → lessons → items (ordered)
4. `onDemandLectureVideos.v1/<course>~<item>` → `sources.byResolution[res].mp4VideoUrl` + `subtitlesVtt`
5. `onDemandSupplements.v1/<course>~<item>` → reading HTML (+ `assets.v1` for attached files)
6. `onDemandLtiDescriptions.v1/<user>~<course>~<item>` → lab instructions + notebook links

---

## 8. Disclaimer & License

This is an **educational tool** for learners to keep **personal, offline copies of
courses they are legitimately enrolled in**. It does not bypass payment, DRM, or any
access control — it uses your own authenticated session, the same one your browser
uses, and can only reach content your account already has access to.

**You** are responsible for how you use it:

- Use it only for courses you're enrolled in, for your own study.
- **Do not redistribute** downloaded videos, readings, or notebooks. That content
  remains the property of the course providers (e.g. IBM, DeepLearning.AI,
  universities) and Coursera, under their own licenses.
- Review Coursera's Terms of Use. Automated/bulk downloading may be restricted;
  using this tool is at your own discretion and risk.

The author provides this software "as is", with no warranty, and accepts no liability
for how it is used (see the full MIT terms in [`LICENSE`](LICENSE)).

Not affiliated with, endorsed by, or connected to Coursera, IBM, or any course
provider.

### License

Released under the [MIT License](LICENSE).
