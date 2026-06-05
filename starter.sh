curl -LsSf https://astral.sh/uv/install.sh | sh
# Download and install nvm:
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
# in lieu of restarting the shell
\. "$HOME/.nvm/nvm.sh"
# Download and install Node.js:
nvm install 24
# Verify the Node.js version:
node -v # Should print "v24.16.0".
# Verify npm version:
npm -v # Should print "11.13.0".
uv sync
uv run ai-token-analyzer init-storage
uv run ai-token-analyzer start-collector --interval-seconds 60