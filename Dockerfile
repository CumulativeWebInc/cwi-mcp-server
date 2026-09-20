# cwi-mcp-server — HTTP bridge container
# Node 20 + python3 (trust_verdict and needledrop_verify shell out to python3).
# PORT is injected by the host (HF Spaces: 7860, Render: $PORT); BIND 0.0.0.0.
FROM node:20-slim
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json ./
COPY server.js server-http.js ./
COPY vendor ./vendor
ENV BIND=0.0.0.0 PORT=7860
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||7860)+'/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
CMD ["node", "server-http.js"]
