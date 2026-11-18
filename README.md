# Radar de Voos — cliente extra

Coleta preços de passagens (Google Voos via SerpApi) para as rotas de `rotas.json`,
guarda tudo em `historico.csv` e publica `precos-extra.json`, que a página
https://docs.ufpr.br/~jefer/radarvoos/radar-de-voos.html lê junto com o JSON do cliente da universidade.

- **Rotas:** edite `rotas.json` (janela da ida, noites, prioridade, teto de preço).
- **Cota:** a conta SerpApi gratuita tem 250 consultas/mês, compartilhadas com o cliente da
  universidade. Este cliente gasta no máximo `POR_RODADA` (3) por dia e `LIMITE_MENSAL` (120) por mês,
  e para sozinho se o saldo da conta cair abaixo de 15.
- **Local:** `python radar_extra.py` (lê a chave do `.env`), `--simular` não gasta cota, `--saldo` só mostra o saldo.
- **GitHub Actions:** roda todo dia às 09:15; a chave fica em Settings › Secrets › Actions › `SERPAPI_KEY`.
