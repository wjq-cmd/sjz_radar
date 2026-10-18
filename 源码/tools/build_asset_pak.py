"""Build the Nva resource package and uncompressed 3D model directory."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import shutil
import zipfile


REQUIRED = ("map_catalog.json", "map_markers.json", "items", "maps2d", "m3d/manifest.json")


def validate(source: Path) -> None:
    missing = [name for name in REQUIRED if not (source / name).exists()]
    if missing:
        raise FileNotFoundError("missing asset inputs: " + ", ".join(missing))


def build_pak(source: Path, output: Path, version: str) -> dict:
    files = sorted(path for path in source.rglob("*") if path.is_file() and "m3d" not in path.relative_to(source).parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    total = sum(path.stat().st_size for path in files)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as package:
        for path in files:
            package.write(path, path.relative_to(source).as_posix())
        package.writestr("nva-assets.json", json.dumps({
            "schema": "nva-assets/v1",
            "version": version,
            "files": len(files),
            "bytes": total,
            "modelsExternal": True,
        }, ensure_ascii=False, separators=(",", ":")))
    temporary.replace(output)
    return {"files": len(files), "bytes": total, "packageBytes": output.stat().st_size}


def build_models(source: Path, destination: Path) -> dict:
    manifest_path = source / "m3d/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    destination.mkdir(parents=True, exist_ok=True)
    expected = {"manifest.json"}
    shutil.copy2(manifest_path, destination / "manifest.json")
    total = 0
    for item in manifest.get("maps", {}).values():
        name = item.get("file")
        if not name or Path(name).name != name or not name.lower().endswith(".glb"):
            raise ValueError("invalid model filename in manifest: %r" % name)
        expected.add(name)
        plain = source / "m3d" / name
        compressed = source / "m3d" / (name + ".gz")
        target = destination / name
        if plain.is_file():
            shutil.copy2(plain, target)
        elif compressed.is_file():
            with gzip.open(compressed, "rb") as reader, target.open("wb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
        else:
            raise FileNotFoundError("missing 3D model: " + name)
        with target.open("rb") as model:
            header = model.read(4)
        if header != b"glTF":
            raise ValueError("invalid GLB header: " + name)
        total += target.stat().st_size
    for stale in destination.iterdir():
        if stale.is_file() and stale.name not in expected:
            stale.unlink()
    return {"files": len(expected) - 1, "bytes": total}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--pak", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    validate(source)
    result = {
        "pak": build_pak(source, args.pak.resolve(), args.version),
        "models": build_models(source, args.models.resolve()),
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
