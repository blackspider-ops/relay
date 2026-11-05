class RelayCodex < Formula
  desc "Keep Codex moving between your own accounts"
  homepage "https://github.com/blackspider-ops/relay"
  url "https://github.com/blackspider-ops/relay/releases/download/v0.7.0/relay-0.7.0.tar.gz"
  sha256 "91d565ea1eba1abb00f40ba56eb3820c44227bc92bec8055d336cf7f70a0c88f"
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
