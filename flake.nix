{
  description = "A tool for parsing certificate lists and building trust stores";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/22.11";
    mach-nix.url = "mach-nix/3.5.0";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = {self, nixpkgs, mach-nix, flake-utils, ...}@attr:
    flake-utils.lib.eachDefaultSystem( system: 
      let pkgs = nixpkgs.legacyPackages.${system};
        prodReq = ''
          ninja
          attrs
          importlab
          six
          toml
          typed-ast
          pybind11
        '';
        devReq = ''
          pre-commit
          black
          isort
          pytest
          pytype
          pytest-cov
          pyasn1
          flit
        '';
        version = "python39";
        buildcatrust = mach-nix.lib.${system}.buildPythonPackage {
          python = version;
          requirements = prodReq; 
          src = ./.;
          packagesExtra = with pkgs; [openssl ninja];
        };
        buildcatrust_dev = mach-nix.lib.${system}.mkPython {
          python = version;
          requirements = (prodReq + devReq);
          packagesExtra = with pkgs; [openssl ninja];
        };
       in rec {
        packages.default = buildcatrust;
        devShells.default = pkgs.mkShell {
          nativeBuildInputs = [ buildcatrust_dev ];
          shellHook = ''
            cd ~/code/buildcatrust
          '';
        };
      }
    );

}