class RelayCodex < Formula
  desc "Keep Codex moving between your own accounts"
  homepage "https://github.com/blackspider-ops/relay"
  url "https://github.com/blackspider-ops/relay/releases/download/v0.7.4/relay-0.7.4.tar.gz"
  sha256 "8f8232553fcefffa95558729e377e101b0ecba807083d0a662af4e02a2feba2c"
  license "MIT"

  depends_on :macos
  depends_on "node"
  depends_on "python@3.13"

  def install
    libexec.install Dir["*"]
    (bin/"relay").write_env_script libexec/"bin/relay.cjs",
      RELAY_PYTHON: formula_opt_bin("python@3.13")/"python3.13"
  end

  def caveats
    <<~EOS
      Relay needs the Codex CLI. If you have not installed it:
        npm install -g @openai/codex

      Then sign into your own accounts:
        relay setup --count 2
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/relay --version")
  end
end
