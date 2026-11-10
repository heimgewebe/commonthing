import type { ExpressionSpecification, StyleSpecification } from "maplibre-gl";

/**
 * Colour grade of the map canvas.
 *
 * The map used to tint the WebGL canvas with the CSS filter
 * `grayscale(0.2) saturate(0.75) brightness(1.03) contrast(0.95)`. A CSS
 * filter on a full-screen canvas costs the compositor one extra full-screen
 * pass for every rendered map frame (measured in headless Chromium: about
 * 16 ms per frame at 1440x900). The grade is affine per channel, so it is
 * applied to each colour before MapLibre draws it instead. Chromium evaluates
 * the filter chain in sRGB and clamps after every function; doing the same
 * here reproduces its output exactly for opaque colours.
 */

type Rgb = [number, number, number];
type Matrix = [Rgb, Rgb, Rgb];

const GRAYSCALE_AMOUNT = 0.2;
const SATURATE_AMOUNT = 0.75;
const BRIGHTNESS_AMOUNT = 1.03;
const CONTRAST_AMOUNT = 0.95;

function grayscaleMatrix(amount: number): Matrix {
  const a = 1 - amount;
  return [
    [0.2126 + 0.7874 * a, 0.7152 - 0.7152 * a, 0.0722 - 0.0722 * a],
    [0.2126 - 0.2126 * a, 0.7152 + 0.2848 * a, 0.0722 - 0.0722 * a],
    [0.2126 - 0.2126 * a, 0.7152 - 0.7152 * a, 0.0722 + 0.9278 * a],
  ];
}

function saturateMatrix(s: number): Matrix {
  return [
    [0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s],
    [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s],
    [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s],
  ];
}

const GRAYSCALE = grayscaleMatrix(GRAYSCALE_AMOUNT);
const SATURATE = saturateMatrix(SATURATE_AMOUNT);

const clamp01 = (value: number) => Math.min(1, Math.max(0, value));

function applyMatrix(matrix: Matrix, [r, g, b]: Rgb): Rgb {
  return matrix.map((row) =>
    clamp01(row[0] * r + row[1] * g + row[2] * b),
  ) as Rgb;
}

export function gradeRgb(rgb: Rgb): Rgb {
  const gray = applyMatrix(GRAYSCALE, rgb);
  const saturated = applyMatrix(SATURATE, gray);
  const bright = saturated.map((v) => clamp01(v * BRIGHTNESS_AMOUNT));
  return bright.map((v) =>
    clamp01(v * CONTRAST_AMOUNT + 0.5 - 0.5 * CONTRAST_AMOUNT),
  ) as Rgb;
}

const HEX_COLOR = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const RGB_COLOR =
  /^rgba?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*(?:,\s*(\d*(?:\.\d+)?)\s*)?\)$/i;

type ParsedColor = { rgb: Rgb; alpha: number | null };

function parseColor(value: string): ParsedColor | null {
  const trimmed = value.trim();
  const hex = HEX_COLOR.exec(trimmed);
  if (hex) {
    let digits = hex[1];
    if (digits.length <= 4) {
      digits = [...digits].map((d) => d + d).join("");
    }
    const channel = (offset: number) =>
      parseInt(digits.slice(offset, offset + 2), 16) / 255;
    return {
      rgb: [channel(0), channel(2), channel(4)],
      alpha: digits.length === 8 ? channel(6) : null,
    };
  }
  const rgb = RGB_COLOR.exec(trimmed);
  if (rgb) {
    const channel = (raw: string) => Math.min(255, Number(raw)) / 255;
    return {
      rgb: [channel(rgb[1]), channel(rgb[2]), channel(rgb[3])],
      alpha: rgb[4] === undefined ? null : clamp01(Number(rgb[4])),
    };
  }
  return null;
}

const toByte = (value: number) => Math.round(value * 255);

/**
 * Returns the graded colour, or the input unchanged when it is not a hex or
 * rgb()/rgba() colour (expression operators, property names, enum values).
 */
export function gradeMapCanvasColor(value: string): string {
  const parsed = parseColor(value);
  if (!parsed) return value;
  const [r, g, b] = gradeRgb(parsed.rgb).map(toByte);
  if (parsed.alpha === null) {
    return `#${[r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("")}`;
  }
  return `rgba(${r}, ${g}, ${b}, ${Number(parsed.alpha.toFixed(4))})`;
}

const GRADE_INPUT_VAR = "canvas_grade_input";

type Expression = unknown[];

/**
 * Wraps a data-driven colour expression (e.g. `["get", "color"]`) so that
 * MapLibre applies the same grade per feature. Feature properties stay the
 * original colours; only the painted value is graded.
 */
export function gradeColorExpression(
  input: ExpressionSpecification,
): ExpressionSpecification {
  const channel = (i: number): Expression => [
    "/",
    ["at", i, ["var", GRADE_INPUT_VAR]],
    255,
  ];
  const clamp = (value: unknown): Expression => ["min", 1, ["max", 0, value]];
  const matrixStep = (matrix: Matrix, vars: string[]): Expression[] =>
    matrix.map((row) =>
      clamp([
        "+",
        ["*", row[0], ["var", vars[0]]],
        ["*", row[1], ["var", vars[1]]],
        ["*", row[2], ["var", vars[2]]],
      ]),
    );
  const out = ["o_r", "o_g", "o_b"];
  const result: Expression = [
    "rgba",
    ["*", 255, ["var", out[0]]],
    ["*", 255, ["var", out[1]]],
    ["*", 255, ["var", out[2]]],
    ["at", 3, ["var", GRADE_INPUT_VAR]],
  ];
  const bindStep = (
    names: string[],
    values: Expression[],
    body: Expression,
  ): Expression => [
    "let",
    names[0],
    values[0],
    names[1],
    values[1],
    names[2],
    values[2],
    body,
  ];
  const linear = ["l_r", "l_g", "l_b"];
  const gray = ["g_r", "g_g", "g_b"];
  const sat = ["s_r", "s_g", "s_b"];
  const bright = ["b_r", "b_g", "b_b"];
  const contrast = bright.map((name) =>
    clamp([
      "+",
      ["*", CONTRAST_AMOUNT, ["var", name]],
      0.5 - 0.5 * CONTRAST_AMOUNT,
    ]),
  );
  const expression: Expression = [
    "let",
    GRADE_INPUT_VAR,
    ["to-rgba", ["to-color", input]],
    bindStep(
      linear,
      [channel(0), channel(1), channel(2)],
      bindStep(
        gray,
        matrixStep(GRAYSCALE, linear),
        bindStep(
          sat,
          matrixStep(SATURATE, gray),
          bindStep(
            bright,
            sat.map((name) => clamp(["*", BRIGHTNESS_AMOUNT, ["var", name]])),
            bindStep(out, contrast, result),
          ),
        ),
      ),
    ),
  ];
  return expression as ExpressionSpecification;
}

function isGradeExpression(value: unknown[]): boolean {
  return value[0] === "let" && value[1] === GRADE_INPUT_VAR;
}

function gradeValue(value: unknown): unknown {
  if (typeof value === "string") return gradeMapCanvasColor(value);
  if (Array.isArray(value)) {
    return isGradeExpression(value) ? value : value.map(gradeValue);
  }
  return value;
}

/**
 * Grades every colour-valued paint property of a style (literal colours and
 * colour literals inside expressions or legacy stop functions). Colours read
 * from feature data are not covered here; wrap them with
 * `gradeColorExpression`.
 */
export function gradeStyleColors(
  style: StyleSpecification,
): StyleSpecification {
  return {
    ...style,
    layers: style.layers.map((layer) => {
      if (!("paint" in layer) || !layer.paint) return layer;
      const paint: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(layer.paint)) {
        paint[key] = key.endsWith("-color") ? gradePaintValue(value) : value;
      }
      return { ...layer, paint } as typeof layer;
    }),
  };
}

function gradePaintValue(value: unknown): unknown {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const fn = value as { stops?: unknown[] };
    if (Array.isArray(fn.stops)) {
      return {
        ...fn,
        stops: fn.stops.map((stop) =>
          Array.isArray(stop) ? [stop[0], gradeValue(stop[1])] : stop,
        ),
      };
    }
    return value;
  }
  return gradeValue(value);
}

/** Grades the colour-valued entries of an overlay layer's paint object. */
export function gradeMapPaint<T extends object | undefined>(paint: T): T {
  if (!paint) return paint;
  const graded: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(paint)) {
    graded[key] = key.endsWith("-color") ? gradePaintValue(value) : value;
  }
  return graded as T;
}

/** `transformStyle` hook for `map.setStyle` that grades the basemap. */
export function gradeBasemapStyle(
  _previous: StyleSpecification | undefined,
  next: StyleSpecification,
): StyleSpecification {
  return gradeStyleColors(next);
}
