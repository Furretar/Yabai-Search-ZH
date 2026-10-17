{
  # Human-readable description of what this flake provides.
  description = "Yabai-Chaci - a CC-CEDICT Chinese-English dictionary";

  # Pin nixpkgs to the same release the host system uses, so libraries match.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      # Target architecture (change for an ARM NAS, e.g. "aarch64-linux").
      system = "x86_64-linux";
      # Alias for the package set built for that system.
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      # `nix develop` drops you into this shell with the tools below on PATH.
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python313 # the interpreter
          uv # dependency manager and command runner
          git # version control
          sqlite # CLI for inspecting the database by hand
        ];

        # The opencc Python wheel links against libstdc++, which NixOS keeps off
        # the default library path, so expose the C++ runtime to it here.
        LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];

        # Printed each time the shell starts, as a reminder of the commands.
        shellHook = ''
          echo "Yabai-Chaci dev shell"
          echo "  first run:   uv sync"
          echo "  import data: uv run python -m scripts.import_cedict"
          echo "  run server:  uv run uvicorn yabai_chaci.main:app --reload"
        '';
      };
    };
}
