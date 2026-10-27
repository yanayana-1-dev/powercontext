"""种子数据入库脚本：把 seed_docs/*.md 导入 seekdb。

用法（在 app 目录下）：python ../scripts/seed.py   # 或指定路径 python seed.py <seed_docs_dir>
前置：seekdb 已启动；embedding 服务已启动（入库需要向量化）。
"""
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from seekmem import ingest, schema  # noqa: E402

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def parse_doc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    title, tags = path.stem, ""
    m = _FM_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip()
            elif line.startswith("tags:"):
                tags = line.split(":", 1)[1].strip()
        text = text[m.end():]
    return {"title": title, "tags": tags, "content": text.strip(), "source": path.name}


def main(seed_dir: Path) -> None:
    schema.ensure_schema()
    files = sorted(seed_dir.glob("*.md"))
    if not files:
        print(f"no seed docs in {seed_dir}")
        return
    print(f"importing {len(files)} docs from {seed_dir} ...")
    ok = 0
    for f in files:
        doc = parse_doc(f)
        try:
            r = ingest.ingest_document(doc["title"], doc["source"], doc["tags"], doc["content"])
            print(f"  ✓ {r['title']}  ({r['chunks']} chunks)")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {doc['title']}: {exc}")
    print(f"done: {ok}/{len(files)}")


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "seed_docs"
    main(d)
