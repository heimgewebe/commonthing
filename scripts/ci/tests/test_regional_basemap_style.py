from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
STYLE_PATH = REPO / "map-style" / "style.json"
STYLE_DARK_PATH = REPO / "map-style" / "style-dark.json"
STYLE_VERSION_PATH = (
    REPO / "apps" / "web" / "src" / "lib" / "map" / "basemapStyleVersion.ts"
)
COLORS_PATH = REPO / "map-style" / "colors.json"
UP_SCRIPT_PATH = REPO / "scripts" / "weltgewebe-up"
WORKFLOW_PATH = REPO / ".github" / "workflows" / "basemap-runtime-proof.yml"
PACKAGE_PATH = REPO / "apps" / "web" / "package.json"
CADDY_PATHS = (
    REPO / "infra" / "caddy" / "Caddyfile",
    REPO / "infra" / "caddy" / "Caddyfile.heim",
    REPO / "infra" / "caddy" / "Caddyfile.vps",
    REPO / "infra" / "caddy" / "Caddyfile.proof",
)


class RegionalBasemapStyleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.style = json.loads(STYLE_PATH.read_text(encoding="utf-8"))
        self.style_dark = json.loads(STYLE_DARK_PATH.read_text(encoding="utf-8"))
        self.colors = json.loads(COLORS_PATH.read_text(encoding="utf-8"))

    def test_style_declares_all_regional_pmtiles_sources(self) -> None:
        sources = self.style["sources"]
        self.assertEqual(
            sources["basemap"]["url"],
            "pmtiles://basemap-hamburg.pmtiles",
        )
        self.assertEqual(
            sources["basemap-schleswig-holstein"]["url"],
            "pmtiles://basemap-schleswig-holstein.pmtiles",
        )
        self.assertEqual(self.style_dark["sources"], sources)

    def test_each_regional_source_has_the_same_visual_layer_set(self) -> None:
        layers = self.style["layers"]
        hamburg = {
            layer["id"]
            for layer in layers
            if layer.get("source") == "basemap"
        }
        schleswig_holstein = {
            layer["id"].removesuffix("-schleswig-holstein")
            for layer in layers
            if layer.get("source") == "basemap-schleswig-holstein"
        }

        self.assertEqual(
            hamburg,
            {
                "landcover",
                "landuse",
                "water",
                "roads",
                "buildings",
                "place-labels",
            },
        )
        self.assertEqual(schleswig_holstein, hamburg)

    def test_style_version_matches_cache_busting_web_contract(self) -> None:
        basemap_module = (
            REPO / "apps" / "web" / "src" / "lib" / "map" / "basemap.ts"
        ).read_text(encoding="utf-8")
        version = self.style["metadata"]["weltgewebe:version"]
        self.assertEqual(version, "0.4.0")
        self.assertEqual(
            self.style_dark["metadata"]["weltgewebe:version"], version
        )
        self.assertEqual(
            self.style["metadata"]["weltgewebe:colorScheme"], "light"
        )
        self.assertEqual(
            self.style_dark["metadata"]["weltgewebe:colorScheme"], "dark"
        )
        self.assertEqual(
            self.style["metadata"]["weltgewebe:darkStyleSha256"],
            hashlib.sha256(STYLE_DARK_PATH.read_bytes()).hexdigest(),
        )
        style_version_module = STYLE_VERSION_PATH.read_text(encoding="utf-8")
        self.assertIn(
            f'LOCAL_BASEMAP_STYLE_VERSION = "{version}"', style_version_module
        )
        self.assertIn('from "./basemapStyleVersion"', basemap_module)
        self.assertIn("style-dark.json", basemap_module)
        self.assertIn("LOCAL_BASEMAP_STYLE_DARK_URL", basemap_module)
        self.assertIn("resolveBasemapStyle", basemap_module)

        self.assertIn(
            'import { BUILD_VERSION } from "$lib/generated/buildVersion";',
            basemap_module,
        )
        generator = (
            REPO / "apps" / "web" / "scripts" / "generate-version.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'const clientModuleFile = path.join(clientDir, "buildVersion.ts")',
            generator,
        )
        self.assertIn('"export const BUILD_VERSION = "', generator)
        package = json.loads(
            (REPO / "apps" / "web" / "package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            package["scripts"]["generate:client-version"],
            "node scripts/generate-version.js --client",
        )
        self.assertIn(
            "pnpm run generate:client-version",
            package["scripts"]["pretest:unit"],
        )

    def test_regional_land_layers_are_visible_and_class_driven(self) -> None:
        layers = {layer["id"]: layer for layer in self.style["layers"]}
        for layer_id in (
            "landcover",
            "landcover-schleswig-holstein",
            "landuse",
            "landuse-schleswig-holstein",
        ):
            with self.subTest(layer=layer_id):
                layer = layers[layer_id]
                self.assertEqual(layer["type"], "fill")
                self.assertGreater(layer["paint"]["fill-opacity"], 0.5)
                expression = layer["paint"]["fill-color"]
                self.assertEqual(expression[:2], ["match", ["get", "class"]])
                self.assertGreaterEqual(len(expression), 8)

    def test_land_layer_colors_match_the_canonical_palette(self) -> None:
        layers = {layer["id"]: layer for layer in self.style["layers"]}
        for category in ("landcover", "landuse"):
            expression = layers[category]["paint"]["fill-color"]
            pairs = dict(zip(expression[2:-1:2], expression[3:-1:2]))
            palette = self.colors["theme"][category]
            self.assertEqual(pairs, {k: v for k, v in palette.items() if k != "default"})
            self.assertEqual(expression[-1], palette["default"])

    def test_dark_land_layer_colors_match_the_dark_palette(self) -> None:
        layers = {layer["id"]: layer for layer in self.style_dark["layers"]}
        for category in ("landcover", "landuse"):
            expression = layers[category]["paint"]["fill-color"]
            pairs = dict(zip(expression[2:-1:2], expression[3:-1:2]))
            palette = self.colors["theme_dark"][category]
            self.assertEqual(
                pairs, {k: v for k, v in palette.items() if k != "default"}
            )
            self.assertEqual(expression[-1], palette["default"])
        self.assertEqual(
            layers["background"]["paint"]["background-color"],
            self.colors["theme_dark"]["background"],
        )
        for road_layer in ("roads", "roads-schleswig-holstein"):
            self.assertEqual(
                layers[road_layer]["paint"]["line-color"],
                self.colors["theme_dark"]["roads"],
            )
        # Dark basemap stays dark; no CSS-filter inversion of the light style.
        light_bg = self.colors["theme"]["background"]
        dark_bg = self.colors["theme_dark"]["background"]
        self.assertNotEqual(light_bg, dark_bg)

    def test_dark_style_preserves_layer_structure(self) -> None:
        light_ids = [layer["id"] for layer in self.style["layers"]]
        dark_ids = [layer["id"] for layer in self.style_dark["layers"]]
        self.assertEqual(light_ids, dark_ids)
        self.assertEqual(
            {k: v.get("url") for k, v in self.style["sources"].items()},
            {k: v.get("url") for k, v in self.style_dark["sources"].items()},
        )

    def test_visual_proofs_follow_the_shared_style_version(self) -> None:
        proof_dir = REPO / "apps" / "web" / "tests" / "proofs"
        for filename in (
            "basemap-real-hamburg-visual.proof.ts",
            "basemap-real-schleswig-holstein-visual.proof.ts",
            "basemap-real-germany-visual.proof.ts",
        ):
            with self.subTest(filename=filename):
                proof = (proof_dir / filename).read_text(encoding="utf-8")
                self.assertIn("LOCAL_BASEMAP_STYLE_VERSION", proof)
                self.assertNotIn("v=0.3.1", proof)

    def test_layer_ids_are_unique_and_sources_exist(self) -> None:
        sources = self.style["sources"]
        layer_ids = [layer["id"] for layer in self.style["layers"]]
        self.assertEqual(len(layer_ids), len(set(layer_ids)))
        for layer in self.style["layers"]:
            if "source" in layer:
                self.assertIn(layer["source"], sources)

    def test_deploy_readiness_checks_each_regional_metadata_alias(self) -> None:
        script = UP_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "for regional_meta in basemap-hamburg.meta.json "
            "basemap-schleswig-holstein.meta.json; do",
            script,
        )
        expected_url = (
            '"https://commonthing.home.arpa/local-basemap/'
            + chr(36)
            + '{regional_meta}"'
        )
        self.assertIn(expected_url, script)

    def test_content_proof_preserves_metadata_hash_variables(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'META_PATH="build/basemap/${META_NAME}"',
            workflow,
        )
        self.assertIn('"${META_PATH}"', workflow)
        self.assertNotIn('META_PATH="build/basemap/"', workflow)

    def test_content_jobs_run_bounded_deep_validation(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        content_jobs = workflow[
            workflow.index("  basemap-pmtiles-content-proof:") :
            workflow.index("  basemap-visual-proof:")
        ]

        self.assertIn("pnpm test:pmtiles-validator", content_jobs)
        self.assertIn(
            "--archive hamburg=../../build/basemap/"
            "basemap-hamburg-v0.1.0.pmtiles",
            content_jobs,
        )
        self.assertIn(
            "--archive schleswig-holstein=../../build/basemap/"
            "basemap-schleswig-holstein-v0.1.0.pmtiles",
            content_jobs,
        )
        self.assertIn(
            "/tmp/hamburg-pmtiles-deep-validation.json", content_jobs
        )
        self.assertIn(
            "/tmp/schleswig-holstein-pmtiles-deep-validation.json",
            content_jobs,
        )

    def test_all_caddy_surfaces_revalidate_mutable_basemap_assets(self) -> None:
        for caddy_path in CADDY_PATHS:
            with self.subTest(caddyfile=caddy_path.name):
                text = caddy_path.read_text(encoding="utf-8")
                self.assertIn(
                    'Cache-Control "no-cache, must-revalidate"', text
                )

        vite_config = (
            REPO / "apps" / "web" / "vite.config.ts"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'res.setHeader("Cache-Control", "no-cache, must-revalidate")',
            vite_config,
        )

    def test_all_caddy_surfaces_declare_pmtiles_content_type(self) -> None:
        for caddy_path in CADDY_PATHS:
            with self.subTest(caddyfile=caddy_path.name):
                text = caddy_path.read_text(encoding="utf-8")
                self.assertIn(
                    'Content-Type "application/octet-stream"', text
                )


    def test_runtime_proof_triggers_on_basemap_runtime_inputs(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        for trigger_path in (
            "apps/web/basemap-mode.policy.json",
            "apps/web/scripts/basemap-mode-resolve.mjs",
            "apps/web/scripts/generate-basemap-config.js",
            "apps/web/src/**",
            "apps/web/tests/fixtures/**",
            "apps/web/playwright.config.ts",
        ):
            with self.subTest(trigger_path=trigger_path):
                self.assertEqual(
                    workflow.count(f'      - "{trigger_path}"'),
                    2,
                    f"expected pull_request and push coverage for {trigger_path}",
                )

    def test_regional_proof_command_pins_regional_build_variant(self) -> None:
        package = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
        scripts = package["scripts"]
        for script_name in (
            "pretest:proof:basemap-real",
            "test:proof:basemap-real",
        ):
            with self.subTest(script_name=script_name):
                command = scripts[script_name]
                self.assertIn("PUBLIC_BASEMAP_MODE=local-sovereign", command)
                self.assertIn("PUBLIC_BASEMAP_VARIANT=regional", command)

    def test_visual_proof_pins_regional_build_variant(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        visual_job = workflow[workflow.index("  basemap-visual-proof:") :]

        self.assertIn("PUBLIC_BASEMAP_MODE: local-sovereign", visual_job)
        self.assertIn("PUBLIC_BASEMAP_VARIANT: regional", visual_job)

    def test_visual_proof_materializes_both_regional_sources(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        visual_job = workflow[workflow.index("  basemap-visual-proof:") :]

        self.assertIn(
            "      - basemap-schleswig-holstein-content-proof",
            visual_job,
        )
        self.assertIn(
            "name: basemap-schleswig-holstein-content-proof-evidence",
            visual_job,
        )
        self.assertIn(
            "for region in hamburg schleswig-holstein; do",
            visual_job,
        )
        self.assertIn(
            '"build/basemap/basemap-${region}-v0.1.0.pmtiles"',
            visual_job,
        )
        self.assertIn(
            "build/basemap/basemap-schleswig-holstein-v0.1.0.pmtiles",
            workflow[: workflow.index("  basemap-visual-proof:")],
        )

    def test_visual_proof_materializes_glyphs_and_publishes_both_evidence_sets(
        self,
    ) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        visual_job = workflow[workflow.index("  basemap-visual-proof:") :]

        self.assertIn(
            "bash scripts/basemap/fetch-glyphs.sh",
            visual_job,
        )
        self.assertIn(
            'test -s "map-style/glyphs/Noto Sans Regular/0-255.pbf"',
            visual_job,
        )
        self.assertIn(
            "build/proofs/basemap-visual/*.png",
            visual_job,
        )
        self.assertIn(
            "build/proofs/basemap-visual/*.json",
            visual_job,
        )
        self.assertIn(
            'summary_dir / "proof-summary.json"',
            visual_job,
        )
        self.assertIn(
            'summary_dir / "schleswig-holstein-proof-summary.json"',
            visual_job,
        )


if __name__ == "__main__":
    unittest.main()
