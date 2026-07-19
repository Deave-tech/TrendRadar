# Base image ships Chromium + all system deps. Its tag MUST match the
# playwright version in package.json (browser binaries are version-pinned).
FROM mcr.microsoft.com/playwright:v1.61.1-noble

WORKDIR /app
COPY --chown=pwuser:pwuser package*.json ./
RUN npm ci --omit=dev || npm install --omit=dev
COPY --chown=pwuser:pwuser . .

# The extension repo is mounted read-only at /bpc (see README).
ENV BPC_DIR=/bpc \
    PROFILE_DIR=/home/pwuser/.bpc-profile \
    PORT=8080

USER pwuser
EXPOSE 8080
CMD ["node", "index.mjs"]
