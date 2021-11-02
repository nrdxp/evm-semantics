{ pkgs ? import inputs."nixpkgs" { }, inputs ? import ./nix/sources.nix { }
, release ? null, kframework ? let
  tag = pkgs.lib.fileContents ./deps/k_release;
  url = "https://github.com/kframework/k/releases/download/${tag}/release.nix";
  args = import (builtins.fetchurl { inherit url; sha256 = "1nbnw6ww386xnmgy8mbbxyyjzpzkg6dpp2d989p94094gahrcqgh"; });
  src = pkgs.fetchgit args;
in import src {
  release = if release == null then pkgs.stdenv.isLinux else false;
}

}:
let

  inherit (pkgs) lib;

  inherit (kframework) k haskell-backend clang;
  # The following llvm-backend directory is needed at build time by kevm, but it's missing
  # from the llvm-backend nix package, so we override the postInstall phase to copy it in
  # the nix store.
  # NOTE: Move this to the llvm-backend repository?
  llvm-backend = kframework.llvm-backend.overrideAttrs (old: {
    postInstall = if old ? postInstall then
      old.postInstall
    else
      "" + ''
        mkdir -p $out/lib/cmake/kframework
        cp -r ../cmake/* $out/lib/cmake/kframework/;
      '';
  });
  llvmPackages = pkgs.llvmPackages_10;

in let inherit (pkgs) callPackage;

in let
  src = inputs.kevm or ./.;
  libff = callPackage ./nix/libff.nix {
    stdenv = llvmPackages.stdenv;
    src = "${src}/deps/plugin/deps/libff";
  };
  kevm = callPackage ./nix/kevm.nix {
    inherit src;
    inherit libff;
    inherit k haskell-backend llvm-backend clang;
    inherit (pkgs.python2Packages) python;
  };
  default = { inherit kevm; };
in default
