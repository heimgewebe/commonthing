{
  description = "Commonthing: pinned Python 3.12 and uv 0.9.11 development toolchain";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/1f607aa79b6356ed45d3cd3c8079c196e79efa2c";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      python = pkgs.python312;
      uvVersion = "0.9.11";
      toolchainLines = nixpkgs.lib.splitString "\n" (builtins.readFile ./toolchain.versions.yml);

      # The upstream musl binary is self-contained on NixOS; no host uv, pip,
      # global Python packages or imperative installation is necessary.
      uv = pkgs.stdenvNoCC.mkDerivation {
        pname = "commonthing-uv";
        version = uvVersion;
        src = pkgs.fetchurl {
          url = "https://github.com/astral-sh/uv/releases/download/0.9.11/uv-x86_64-unknown-linux-musl.tar.gz";
          hash = "sha256-XMBv5xN0qIg66yyDoUGktfrIWE7olLoxxXkiVFCLTpo=";
        };
        nativeBuildInputs = [ pkgs.gnutar pkgs.gzip ];
        dontUnpack = true;
        installPhase = ''
          runHook preInstall
          mkdir -p "$out/bin"
          tar -xzf "$src" -C "$out/bin" --strip-components=1 \
            uv-x86_64-unknown-linux-musl/uv \
            uv-x86_64-unknown-linux-musl/uvx
          runHook postInstall
        '';
      };
    in
    assert builtins.elem "python: \"3.12\"" toolchainLines;
    assert builtins.elem "uv: \"0.9.11\"" toolchainLines;
    {
      packages.${system}.uv-pinned = uv;

      devShells.${system}.default = pkgs.mkShell {
        packages = [ python uv ];
        shellHook = ''
          export UV_PYTHON="${python}/bin/python3.12"
          export UV_PYTHON_DOWNLOADS=never
        '';
      };

      checks.${system}.toolchain-version = pkgs.runCommand "commonthing-pinned-python-uv-check" {
        nativeBuildInputs = [ python uv pkgs.gnugrep ];
      } ''
        test "$(uv --version)" = "uv 0.9.11"
        python3 --version | grep -F 'Python 3.12.'
        touch "$out"
      '';
    };
}