{
  description = "gas-price-api — free nearby gas station prices for any location, as a static + live JSON API";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;

        # Runtime deps, shared by the devShell and the packaged app.
        pyDeps =
          ps: with ps; [
            httpx
            pydantic
            pyyaml
            fastapi
            uvicorn
            rich
          ];

        pyEnv = python.withPackages (ps: (pyDeps ps) ++ [ ps.pytest ]);

        gasprice = python.pkgs.buildPythonApplication {
          pname = "gasprice";
          version = "0.1.0";
          src = ./.;
          pyproject = true;
          build-system = [ python.pkgs.setuptools ];
          dependencies = pyDeps python.pkgs;
          # Tests live outside the installed package; run them in the devShell.
          doCheck = false;
        };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pyEnv
            pkgs.ruff
            pkgs.git
            pkgs.direnv
            pkgs.jq
            pkgs.gh
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            # bin/gasprice is a thin wrapper, so the CLI works the same here and in CI.
            export PATH="$PWD/bin:$PATH"
            export GASPRICE_CACHE_DIR="$PWD/.cache"
            # Nominatim and Overpass both ask for a contactable User-Agent.
            export GASPRICE_USER_AGENT="''${GASPRICE_USER_AGENT:-gas-price-api/0.1 (+https://github.com/bytehunt/gas-price-api)}"
            [ -f .env.local ] && set -a && . ./.env.local && set +a

            # Only greet a human; `nix develop --command ...` (CI, scripts)
            # should produce nothing but the command's own output.
            if [ -n "''${PS1-}" ]; then
            cat <<'BANNER'
            gas-price-api dev shell

              gasprice fetch "20850"                prices near a ZIP / place / "lat,lon"
              gasprice fetch "Rockville, MD" -r 3    smaller radius
              gasprice cheapest 20850 --fuel diesel  single best price
              gasprice collect                       refresh ./public from locations.yaml
              gasprice serve                         live API on http://127.0.0.1:8000/docs
              pytest -q                              tests
              ruff check . && ruff format .          lint

            BANNER
            fi
          '';
        };

        packages.default = gasprice;
        packages.gasprice = gasprice;

        apps.default = {
          type = "app";
          program = "${gasprice}/bin/gasprice";
        };

        formatter = pkgs.nixfmt-rfc-style;
      }
    );
}
