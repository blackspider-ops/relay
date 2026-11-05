class RelayCodex < Formula
  desc "Keep Codex moving between your own accounts"
  homepage "https://github.com/blackspider-ops/relay"
  url "https://github.com/blackspider-ops/relay/releases/download/v0.5.0/relay-0.5.0.tar.gz"
  sha256 "4b8e154afe56083b5cf9181f69094e5123e0d5361e49ccf6770aeb77991cf7bb"
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
