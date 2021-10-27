let
  sources = import ./nix/sources.nix {};
  pinned = import sources."nixpkgs" {};
in

{ pkgs ? pinned
, release ? null
}:

let release_ = release; in

let
  inherit (pkgs) lib;
  ttuegel = import sources."nix-lib" { inherit pkgs; };

  release = if release_ == null then pkgs.stdenv.isLinux else false;

  kframework = import ./deps/k/default.nix { inherit release; };
  inherit (kframework) k haskell-backend llvm-backend clang;
  llvmPackages = pkgs.llvmPackages_10;
in

let
  inherit (pkgs) callPackage;
in

let
  src = ttuegel.cleanGitSubtree {
    name = "evm-semantics";
    src = ./.;
  };
  libff = callPackage ./nix/libff.nix {
    stdenv = llvmPackages.stdenv;
    src = ttuegel.cleanGitSubtree {
      name = "libff";
      src = ./.;
      subDir = "deps/plugin/deps/libff";
    };
  };
  kevm = callPackage ./nix/kevm.nix {
    inherit src;
    inherit (ttuegel) cleanSourceWith;
    inherit libff;
    inherit k haskell-backend llvm-backend clang;
    inherit (pkgs.python2Packages) python;
  };
  default =
    {
      inherit kevm;
    };
in default
