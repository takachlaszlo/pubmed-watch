"""Where a downloaded PDF belongs (journal / issue folders) and what is already on disk.

The same functions name the files and recognise them again later, so the layout lives in one tested place:
`<folder>/<folder>/.../<year>-<pmid>-<title slug>.pdf`, e.g.
`Pediatr Infect Dis J/2026_vol-45_issue-10/2026-42825830-early-onset-neonatal-sepsis-and-neonatal-antibiotic.pdf`.

n8n's Write File node cannot create folders, so n8n saves every PDF into one flat, always existing inbox folder and
reports it; this service then moves the file to its final journal/issue folder (it knows the volume and issue, and
can correct the folder later when PubMed assigns the issue of an online-first article).
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

FOLDER_KINDS = ("section", "journal", "issue", "year")
DEFAULT_INBOX = "_inbox"
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PMID_IN_NAME = re.compile(r"^[0-9x]{4}-(\d+)-.*\.pdf$", re.I)


def safe_name(text: str, fallback: str = "ismeretlen", maxlen: int = 60) -> str:
    """A folder name that is valid on Windows/SMB and Linux alike."""
    name = _BAD_CHARS.sub(" ", text or "")
    name = " ".join(name.split()).strip(" .")
    name = name[:maxlen].rstrip(" .")
    return name or fallback


def slug(title: str, maxlen: int = 70) -> str:
    ascii_text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:maxlen].strip("-") or "cikk"


def journal_folder(article: dict) -> str:
    """NLM journal abbreviation (as in PubMed citations), without dots."""
    name = (article.get("journal_abbrev") or article.get("journal") or "").replace(".", " ")
    return safe_name(name, "ismeretlen folyoirat")


def _part(text: str) -> str:
    return re.sub(r"\s+", "-", safe_name(text, "", 30))


def issue_folder(article: dict) -> str:
    """`2026_vol-45_issue-10`; articles that PubMed has not assigned to an issue yet go to `2026_online-first`."""
    year = (article.get("pub_year") or "")[:4] or "xxxx"
    volume = _part(article.get("volume") or "")
    issue = _part(article.get("issue") or "")
    if issue.isdigit():
        issue = issue.zfill(2)  # issues sort in order within a volume
    parts = [year]
    if volume:
        parts.append(f"vol-{volume}")
    if issue:
        parts.append(f"issue-{issue}")
    if len(parts) == 1:
        parts.append("online-first")
    return "_".join(parts)


def pdf_filename(article: dict) -> str:
    year = (article.get("pub_year") or "")[:4] or "xxxx"
    return f"{year}-{article['pmid']}-{slug(article.get('title', ''))}.pdf"


def pdf_relpath(article: dict, folders: list[str]) -> str:
    """Path of the PDF relative to the PDF root, with forward slashes."""
    parts: list[str] = []
    for kind in folders:
        if kind == "section":
            parts.append(safe_name(article.get("section", ""), "szekcio"))
        elif kind == "journal":
            parts.append(journal_folder(article))
        elif kind == "issue":
            parts.append(issue_folder(article))
        elif kind == "year":
            parts.append((article.get("pub_year") or "")[:4] or "xxxx")
        else:
            raise ValueError(f"ismeretlen mappa-típus: {kind}")
    parts.append(pdf_filename(article))
    return "/".join(parts)


def scan_pdf_dir(root: Path | str) -> list[tuple[str, str, int, str]]:
    """(pmid, relative path, size, modified ISO time) of every PDF named by our scheme, at any depth.

    Names only: the files are never opened. A PDF that was moved elsewhere in the tree still counts."""
    root = Path(root)
    found: list[tuple[str, str, int, str]] = []
    if not root.is_dir():
        return found
    for folder, _dirs, files in os.walk(root):
        for name in files:
            match = _PMID_IN_NAME.match(name)
            if not match:
                continue
            full = Path(folder) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            modified = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
            found.append((match.group(1), full.relative_to(root).as_posix(), stat.st_size, modified))
    return found


def inbox_relpath(article: dict, inbox: str = DEFAULT_INBOX) -> str:
    return f"{inbox}/{pdf_filename(article)}"


def safe_relpath(rel: str) -> bool:
    """A path we are willing to act on: relative, no `..`, no drive letters, a PDF."""
    if not rel or rel.startswith(("/", "\\")) or ":" in rel or not rel.lower().endswith(".pdf"):
        return False
    return ".." not in Path(rel.replace("\\", "/")).parts


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _prune_empty_parents(root: Path, folder: Path, keep: set[Path]) -> None:
    """Removes folders that our move just emptied (never the root, never a folder in `keep`; rmdir only
    ever removes empty folders)."""
    root = root.resolve()
    folder = folder.resolve()
    while folder != root and folder not in keep and _inside(root, folder):
        try:
            folder.rmdir()
        except OSError:
            return  # not empty (or not ours to remove): stop
        folder = folder.parent


def organize_file(root: Path | str, current: str, target: str, keep_dirs: tuple[str, ...] = ()) -> str:
    """Moves root/current to root/target (creating the folders). Returns the new relative path, or '' if nothing
    was moved. Never overwrites, never leaves `root`, never touches anything that is not a regular file."""
    root = Path(root)
    if not (safe_relpath(current) and safe_relpath(target)):
        log.warning("rendezés kihagyva, gyanús útvonal: %r -> %r", current, target)
        return ""
    src, dst = root / current, root / target
    if not (_inside(root, src) and _inside(root, dst)) or not src.is_file() or src.is_symlink():
        return ""
    if dst.exists():
        log.warning("a cél már létezik, nem írom felül: %s", target)
        return ""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    except OSError as exc:
        log.warning("a PDF áthelyezése nem sikerült (%s -> %s): %s", current, target, exc)
        return ""
    _prune_empty_parents(root, src.parent, {(root / k).resolve() for k in keep_dirs})
    return target


def organize_all(storage, root: Path | str, folders: list[str], inbox: str = DEFAULT_INBOX,
                 only_pmid: str | None = None) -> int:
    """Puts every downloaded PDF into the folder its article belongs to now. Only files still sitting exactly
    where the ledger says are moved, so a PDF you filed yourself is never touched. Returns how many moved."""
    root = Path(root)
    if not root.is_dir():
        return 0
    moved = 0
    for pmid, current, article in storage.downloads_to_organize(only_pmid):
        target = pdf_relpath(article, folders)
        if current == target:
            continue
        final = organize_file(root, current, target, keep_dirs=(inbox,))
        if final:
            storage.set_download_path(pmid, final)
            moved += 1
    if moved:
        storage.commit()
        log.info("PDF-ek rendezve a folyóirat/issue mappákba: %d", moved)
    return moved
