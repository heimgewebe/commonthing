import { describe, expect, it } from "vitest";
import { parseOptionalNodeLocation } from "./nodeLocation";

describe("optional node location", () => {
  it("clears location without turning empty strings into zero coordinates", () => {
    expect(parseOptionalNodeLocation("", " ")).toBeNull();
    expect(parseOptionalNodeLocation("0", "0")).toEqual({ lat: 0, lon: 0 });
  });
  it.each([
    ["", "10"],
    ["53", ""],
    ["NaN", "10"],
    ["Infinity", "0"],
    ["91", "0"],
    ["0", "181"],
  ])("rejects %j", (lat, lon) => {
    expect(parseOptionalNodeLocation(lat, lon)).toBeUndefined();
  });
});
