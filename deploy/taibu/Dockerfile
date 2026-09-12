# syntax=docker/dockerfile:1.7
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build
WORKDIR /app
ENV CI=true
RUN npm install --global pnpm@10.11.0
COPY --from=taibu-src /package.json /pnpm-lock.yaml /pnpm-workspace.yaml ./
COPY --from=taibu-src /packages/core ./packages/core
COPY --from=taibu-src /packages/mcp-server ./packages/mcp-server
RUN node -e "const fs=require('node:fs'); fs.writeFileSync('package.json', JSON.stringify({name:'financeclaw-taibu-build',private:true})); const lock=fs.readFileSync('pnpm-lock.yaml','utf8'); if(!lock.includes('  .:\\n')) throw Error('unsupported upstream lockfile'); fs.writeFileSync('pnpm-lock.yaml',lock.replace(/  \\.:\\n[\\s\\S]*?(?=  packages\\/)/,'  .: {}\\n\\n'));"
RUN rm -rf packages/core/node_modules packages/mcp-server/node_modules \
    && pnpm install --filter taibu-mcp-server... --frozen-lockfile --ignore-scripts
RUN pnpm -C packages/core build && pnpm -C packages/mcp-server build
RUN rm -rf node_modules packages/core/node_modules packages/mcp-server/node_modules \
    && pnpm install --filter taibu-mcp-server... --prod --frozen-lockfile --offline --ignore-scripts \
    && rm -rf packages/core/src packages/core/tests packages/mcp-server/src packages/mcp-server/tests

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32
WORKDIR /app/packages/mcp-server
ENV NODE_ENV=production PORT=3001 MCP_HOST=0.0.0.0 TZ=Asia/Shanghai
COPY --from=build --chown=node:node /app /app
USER node
EXPOSE 3001
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3001/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
CMD ["node", "dist/index.js"]
