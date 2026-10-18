"""Create reproducible Nva 1.1.0 backup archives and package checksums."""

from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile


PACKAGE = Path(__file__).resolve().parents[2]
SOURCE = PACKAGE / "源码"
PROGRAM = PACKAGE / "程序"
BACKUP = PACKAGE / "备份"

SOURCE_EXCLUDED_DIRS = {"bin", "obj", "build", "__pycache__"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_files() -> list[Path]:
    result: list[Path] = []
    protected_three_build = SOURCE / "Parser" / "ErCore" / "web" / "vendor" / "three" / "build"
    for path in SOURCE.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(SOURCE)
        excluded = any(part in SOURCE_EXCLUDED_DIRS for part in relative.parts)
        if excluded and not path.is_relative_to(protected_three_build):
            continue
        result.append(path)
    return sorted(result)


def program_files() -> list[Path]:
    return sorted(path for path in PROGRAM.rglob("*") if path.is_file())


def write_archive(output: Path, root_name: str, base: Path, files: list[Path], compression: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=compression, compresslevel=6 if compression else None) as archive:
        for path in files:
            archive.write(path, (Path(root_name) / path.relative_to(base)).as_posix())
    with zipfile.ZipFile(temporary, "r") as archive:
        bad = archive.testzip()
        if bad is not None:
            raise RuntimeError(f"ZIP verification failed at {bad}")
    temporary.replace(output)


def main() -> None:
    source_zip = BACKUP / "Nva本地离线解析-1.1.0-源码.zip"
    program_zip = BACKUP / "Nva本地离线解析-1.1.0-发布版.zip"
    write_archive(source_zip, "源码", SOURCE, source_files(), zipfile.ZIP_DEFLATED)
    # Keep the already-compressed PAK/EXE and raw GLB files byte-for-byte without another compression layer.
    write_archive(program_zip, "程序", PROGRAM, program_files(), zipfile.ZIP_STORED)

    checksummed = program_files() + [source_zip, program_zip]
    lines = [f"{sha256(path)}  {path.relative_to(PACKAGE).as_posix()}" for path in checksummed]
    (PACKAGE / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"source_zip={source_zip} files={len(source_files())} bytes={source_zip.stat().st_size}")
    print(f"program_zip={program_zip} files={len(program_files())} bytes={program_zip.stat().st_size}")
    print(f"checksums={len(lines)}")


if __name__ == "__main__":
    main()
