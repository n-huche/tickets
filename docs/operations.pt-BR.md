# tickets — monitor de passagens Recife ⇄ Rio de Janeiro

Monitor contínuo de preços **REC → Rio (GIG ou SDU)** e **Rio → REC**. Ele grava cada preço visto num SQLite, combina ida e volta localmente e dispara um webhook quando **ida + volta ≤ R$ 1.100**.

Tudo fica em `/workspace/tickets`. Nada é gravado fora daqui.

## Comandos

```bash
cd /workspace/tickets
./tickets status              # saúde do serviço, última varredura, melhor preço, fontes, erros
./tickets best                # melhores combinações atuais (use --links para ver os links)
./tickets sources             # status de cada fonte (ok / bloqueada / desativada e motivo)
./tickets history             # histórico geral; --ida 2027-01-05 / --volta 2027-01-15 / ambos
./tickets sweeps              # lista das varreduras
./tickets promos              # promoções REC/Rio vistas nos feeds (sinal)
./tickets alerts              # alertas enviados
./tickets logs -f             # acompanha o log (--errors só erros, --json bruto)
./tickets test-alert          # envia um webhook de TESTE
./tickets sweep               # roda uma varredura agora, em primeiro plano
./tickets service start|stop|restart|status|ensure
./tickets dashboard           # gera dashboard/index.html
```

**Dashboard:** o serviço já sobe um servidor em `127.0.0.1:8787`, que escuta só localmente. Do seu computador, abra um túnel:

```bash
ssh -L 8787:127.0.0.1:8787 <box>     # e abra http://localhost:8787
```

Também há `/health`, que devolve JSON com o estado, a idade do heartbeat e o pid.

## Regras da busca (`tickets_app/config.py`)

| Regra | Valor |
|---|---|
| Trecho | ida REC → GIG/SDU e volta GIG/SDU → REC; as cias podem ser diferentes (duas tarifas só de ida) |
| Passageiro | 1 adulto, só bagagem de mão, tarifa básica mais barata |
| Voo | só **direto** (sem escala nem conexão) |
| Horário | decolar **≥ 04:00** e pousar **≤ 23:00** no mesmo dia, nos dois trechos |
| Datas (2027) | ida e volta na **mesma janela**: 01–15/jan, 24/jan–05/fev ou 11–28/fev |
| Estadia | 11 a 15 dias |
| Alerta | total ≤ **R$ 1.100**; datas passadas são ignoradas |

Cada varredura consulta **datas de ida e de volta separadas** (22 + 22 datas). As 95 combinações válidas são calculadas localmente, e só entram tarifas vistas nas últimas 6 h.

### Qual tarifa inclui a bagagem de mão (voos nacionais)

| Cia | Tarifa mais barata | Bagagem de mão |
|---|---|---|
| Azul | "Azul" (básica) | 10 kg + item pessoal |
| GOL | "Light" | 12 kg + item pessoal |
| LATAM | "Light" | 12 kg + item pessoal ("Basic" só existe em rotas internacionais) |

Em voos nacionais, **todas** as tarifas básicas das três cias incluem bagagem de mão. Por isso o menor preço já atende à regra "só mala de mão".

## Fontes

| Fonte | Status | Como |
|---|---|---|
| **Google Flights** (principal) | ✅ funciona | HTTP puro (BRL, gl=BR), 88 consultas por varredura (GIG e SDU separados) |
| Kayak | ✅ funciona | Chrome headless; mostra também Decolar, ViajaNet, 123milhas, MaxMilhas, Booking, Trip.com e as cias como **vendedores** |
| Momondo | ✅ funciona | mesmo motor do Kayak |
| 123milhas | ✅ funciona | Chrome headless (API interna) — **revenda de milhas** |
| MaxMilhas | ✅ funciona | Chrome headless (API interna) — **revenda de milhas** quando a tarifa é "miles" |
| Kiwi.com | ✅ funciona | Chrome headless (GraphQL), em BRL |
| Melhores Destinos | ✅ sinal | RSS filtrado por Recife/Rio |
| Passagens Imperdíveis | ✅ sinal | cards da página inicial filtrados por Recife/Rio |
| Voopter | ✅ sinal | página de promoções saindo de Recife (a busca do site é uma SPA sem URL estável) |
| Skyscanner | ❌ bloqueado | captcha PerimeterX |
| Azul / GOL / LATAM (sites e páginas de promoção) | ❌ bloqueado | Akamai / reCAPTCHA. As tarifas dessas cias chegam pelas outras fontes |
| Decolar | ❌ bloqueado (direto) | 403 + Cloudflare; o preço aparece como vendedor no Kayak/Momondo |
| ViajaNet | ❌ bloqueado (direto) | 403 DataDome; o preço aparece como vendedor no Kayak/Momondo |
| Submarino Viagens, Hurb | ❌ inviável | a conexão TLS é recusada a partir da box |
| Zarpo | ❌ inviável | só vende hotéis e resorts |

- As fontes de navegador são lentas (10–40 s por consulta). Por isso fazem **rodízio**: em cada varredura, só as ~14 datas consultadas há mais tempo. As 44 datas são cobertas a cada ~3 varreduras (~1,5 h).
- Uma fonte que falha fica isolada, sem derrubar as outras. Em caso de bloqueio (403/429/captcha), ela entra em **backoff** de 30–120 min, que dobra a cada bloqueio seguido, até no máximo 12 h.
- Cada observação guarda `source` (onde foi vista), `seller` (quem vende, ex.: "Decolar (via Kayak)") e `seller_kind` (`cia`, `ota`, `metabusca` ou `revenda_milhas`).
- Os **feeds** são só sinal: aparecem em `./tickets promos` e no dashboard e não entram nas combinações.

Para adicionar uma fonte, crie uma classe em `tickets_app/sources/` (subclasse de `Source` ou de `BrowserSource`) e registre-a em `sources/registry.py`.

## Alertas (webhook)

Configure em **`/workspace/tickets/.env`** (chmod 600, fora do git; o modelo é `.env.example`):

```
TICKETS_WEBHOOK_URL=https://...        # URL que recebe o POST JSON
TICKETS_WEBHOOK_AUTH=Bearer xxxxx      # valor COMPLETO do header Authorization
```

O `.env` é relido a cada alerta, então **não é preciso reiniciar** o serviço. Sem URL, o monitor registra um aviso no log e continua rodando.

- Cada combinação (data + voo de ida | data + voo de volta) gera **um** alerta. Ela só volta a alertar se o preço cair mais.
- O payload traz `total_brl`, `dias_no_rio`, `janela`, `ida` e `volta`. Cada trecho tem data, horários, cia, voo, preço, fonte, vendedor, `revenda_milhas` e link.
- Há também `trecho_mais_barato_de_revenda_de_milhas`, `algum_trecho_de_revenda_de_milhas` e `aviso`, que vem preenchido quando algum trecho é vendido pela 123milhas ou pela MaxMilhas.
- Para testar: `./tickets test-alert`.

## Serviço e persistência

- `./tickets service start` sobe `bin/supervisor.sh` em segundo plano (nohup + flock, então só roda uma instância).
- O supervisor reinicia o daemon se ele cair ou se o heartbeat (`data/heartbeat.json`) ficar parado por mais de 5 min. Os reinícios têm backoff.
- O daemon faz uma varredura a cada **30 ± 8 min**.
- **A box não tem systemd nem cron**, então depois de um reboot rode `/workspace/tickets/tickets service start`. O `service ensure` é idempotente e pode ser chamado de qualquer script de inicialização.

## Arquivos

```
tickets                 wrapper da CLI
bin/supervisor.sh       supervisor (watchdog + restart)
tickets_app/            código (config, rules, db, sweep, alerts, daemon, dashboard, cli, sources/)
data/tickets.db         SQLite: sweeps, queries, observations, current_fares, combos, alerts, promos, source_state
data/heartbeat.json     heartbeat do daemon
logs/tickets.log        log legível   | logs/tickets.jsonl  log estruturado
logs/daemon.out         stdout/stderr do daemon | logs/supervisor.log
dashboard/index.html    dashboard estático (regenerado a cada varredura)
```
