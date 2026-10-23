from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
BUILDER = REPO / "scripts" / "basemap" / "build-schleswig-holstein-pmtiles.sh"
INPUT_SHA256 = "3e5efb30488daa87a098d61bf1f60155f89cdcf900aeadee5a1cd27910bfd450"


class SchleswigHolsteinBasemapBuilderTest(unittest.TestCase):
    def test_transient_planetiler_failures_retry_without_reusing_partial_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            script = root / "scripts" / "basemap" / BUILDER.name
            script.parent.mkdir(parents=True)
            shutil.copy2(BUILDER, script)

            # The isolated retry fixture must not download a real 930 MB
            # archive. Dedicated tests cover the pinned production downloader.
            self._write_executable(
                script.parent / "prepare-water-polygons.py",
                """#!/usr/bin/env python3
from pathlib import Path
import sys
source = Path(sys.argv[1]) / "data" / "sources" / "water-polygons-split-3857.zip"
source.parent.mkdir(parents=True, exist_ok=True)
source.write_bytes(b"fixture-water-polygons")
""",
            )

            basemap_dir = root / "build" / "basemap"
            basemap_dir.mkdir(parents=True)
            (basemap_dir / "schleswig-holstein-260101.osm.pbf").write_bytes(b"fixture")

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            count_path = root / "docker-count"
            reuse_path = root / "partial-output-reused"
            partial_path_marker = root / "partial-output-path"
            output_path = basemap_dir / "basemap-schleswig-holstein-v0.1.0.pmtiles"

            self._write_executable(
                fake_bin / "sha256sum",
                f'''#!/usr/bin/env bash
printf '%s  %s\\n' "{INPUT_SHA256}" "$1"
''',
            )
            self._write_executable(
                fake_bin / "sleep",
                "#!/usr/bin/env bash\nexit 0\n",
            )
            self._write_executable(
                fake_bin / "docker",
                '''#!/usr/bin/env bash
set -euo pipefail
output_name=""
for argument in "$@"; do
  case "$argument" in
    --output=/data/*) output_name="${argument#--output=/data/}" ;;
  esac
done
[[ -n "$output_name" ]]
case " $* " in
  *" --water-polygons-path=/data/data/sources/water-polygons-split-3857.zip "*) ;;
  *) echo "water polygons path was not passed" >&2; exit 95 ;;
esac
[[ -s "$MOCK_BASEMAP_DIR/data/sources/water-polygons-split-3857.zip" ]]
output_path="$MOCK_BASEMAP_DIR/$output_name"
printf '%s\\n' "$output_path" > "$MOCK_PARTIAL_PATH_MARKER"

count=0
if [[ -f "$MOCK_DOCKER_COUNT" ]]; then
  count="$(cat "$MOCK_DOCKER_COUNT")"
fi
count=$((count + 1))
printf '%s\\n' "$count" > "$MOCK_DOCKER_COUNT"
if [[ -e "$output_path" ]]; then
  printf 'partial output was reused on attempt %s\\n' "$count" > "$MOCK_REUSE_MARKER"
  exit 91
fi
printf 'partial-%s' "$count" > "$output_path"
if [[ "$count" -lt 3 ]]; then
  exit 1
fi
printf 'PMTiles\\003fixture' > "$output_path"
''',
            )

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "PLANETILER_MAX_ATTEMPTS": "3",
                    "PLANETILER_RETRY_DELAY_SECONDS": "0",
                    "MOCK_DOCKER_COUNT": str(count_path),
                    "MOCK_BASEMAP_DIR": str(basemap_dir),
                    "MOCK_PARTIAL_PATH_MARKER": str(partial_path_marker),
                    "MOCK_REUSE_MARKER": str(reuse_path),
                }
            )

            completed = subprocess.run(
                ["bash", str(script)],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(count_path.read_text(encoding="utf-8").strip(), "3")
            self.assertFalse(reuse_path.exists())
            partial_path = Path(partial_path_marker.read_text(encoding="utf-8").strip())
            self.assertNotEqual(partial_path, output_path)
            self.assertEqual(partial_path.suffix, ".pmtiles")
            self.assertFalse(partial_path.exists())
            self.assertTrue(output_path.read_bytes().startswith(b"PMTiles"))

            metadata = json.loads(
                (basemap_dir / "basemap-schleswig-holstein-v0.1.0.meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["region"], "schleswig-holstein")
            self.assertEqual(metadata["status"], "ready")
            self.assertIn("retrying", completed.stderr)

    def test_publication_masks_interrupts_until_the_pair_is_owned(self) -> None:
        source = BUILDER.read_text(encoding="utf-8")
        ignore_signals = source.index(
            "trap '' INT TERM", source.index("publish_immutable_pair()")
        )
        artifact_link = source.index('ln "$PARTIAL_PMTILES_PATH"', ignore_signals)
        artifact_owned = source.index("FINAL_ARTIFACT_CREATED=1", artifact_link)
        metadata_link = source.index('ln "$PARTIAL_META_PATH"', artifact_owned)
        metadata_owned = source.index("FINAL_META_CREATED=1", metadata_link)
        publication_complete = source.index("PUBLISH_COMPLETE=1", metadata_owned)
        restore_interrupt = source.index("trap on_interrupt INT", publication_complete)
        restore_terminate = source.index("trap on_terminate TERM", restore_interrupt)

        self.assertLess(ignore_signals, artifact_link)
        self.assertLess(artifact_link, artifact_owned)
        self.assertLess(artifact_owned, metadata_link)
        self.assertLess(metadata_link, metadata_owned)
        self.assertLess(metadata_owned, publication_complete)
        self.assertLess(publication_complete, restore_interrupt)
        self.assertLess(restore_interrupt, restore_terminate)

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
