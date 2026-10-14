{
  description = "yabai-dict — a CC-CEDICT Chinese-English dictionary";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python313
          uv
          git
          sqlite
        ];

        shellHook = ''
          echo "yabai-dict dev shell"
          echo "  first run:   uv sync"
          echo "  import data: uv run python -m scripts.import_cedict"
          echo "  run server:  uv run uvicorn yabai_dict.main:app --reload"
        '';
      };
    };
}
