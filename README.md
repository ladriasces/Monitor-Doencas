# Monitor de Doenças por Território

**LADRI — Laboratório de Dados e Relações Internacionais · ASCES-UNITA**

*Em preparação: o endereço do site será divulgado no lançamento, depois da revisão por profissionais da área da saúde.*

## O que é

Uma plataforma aberta para acompanhar, semana a semana, a dengue, a chikungunya, a zika e as doenças respiratórias graves (SRAG: covid-19, influenza e VSR) em cada município e estado do Brasil, e a ligação dessas doenças com o clima. Os dados vêm de fontes oficiais (Ministério da Saúde e Fiocruz) e se atualizam sozinhos. É a plataforma irmã do [Monitor El Niño](https://ladriasces.github.io/Monitor-El-Nino/).

A plataforma **não faz vigilância epidemiológica nem previsões próprias**: ela reúne, explica e mostra no mapa os indicadores oficiais. Os níveis de alerta são os do InfoDengue.

## Como usar

Abra o site e escolha o seu município em **"Escolha seu município"**. O seu lugar aparece em destaque em todas as abas.

| Aba | O que mostra |
|---|---|
| **Agora** | Resumo da semana, o seu município (nível de alerta, casos, série recente), o mapa da dengue e as notícias |
| **Dengue, chikungunya e zika** | Mapa dos 5.570 municípios por nível de alerta ou incidência, série semanal com a estimativa de casos, tabela por estado |
| **Doenças respiratórias** | SRAG por semana e por vírus, comparação com 2025 e com 2023–2025, idade dos pacientes, tendência por estado |
| **Clima e saúde** | Calor dos últimos 30 dias (Monitor El Niño) e dengue das últimas 4 semanas em cada região imediata |
| **Outras doenças** | (em preparação) séries de leptospirose, malária, tuberculose, hanseníase, sífilis e vacinação, para análise |
| **Notícias** | Manchetes dos últimos 14 dias por tema e estado |

Em **Dados e método** estão as fontes, as definições e os limites de cada número.

## Fontes

- **Arboviroses:** InfoDengue (Fiocruz e FGV), pela API do [Mosqlimate](https://mosqlimate.org/) — precisa de uma chave gratuita (`MOSQLIMATE_KEY`).
- **SRAG:** SIVEP-Gripe, Ministério da Saúde, [Portal de Dados Abertos do SUS](https://dadosabertos.saude.gov.br/dataset/srag-2019-a-2026) (arquivos anuais atualizados toda semana).
- **Clima:** Monitor El Niño (LADRI), já publicado.
- **População e mapas:** IBGE.
- **Notícias:** GDELT, Agência Brasil (Saúde), Agência Fiocruz e ONU News (Saúde).

## Privacidade (LGPD)

Os arquivos do SIVEP-Gripe têm uma linha por pessoa notificada (dado pessoal sensível, art. 11 da LGPD). Eles são lidos só para contar casos, dentro da rotina automática, e apagados em seguida (`saude_comum.agregar_srag`). Nada individual vai para o repositório (`.gitignore`) nem para o site; o `04_validar.py` confere isso antes de cada publicação.

## Como funciona

```
saude_comum.py      funções e constantes comuns (semana epidemiológica, agregação do SIVEP-Gripe, downloads)
00_referencias.py   tabelas fixas: municípios, regiões imediatas, população, malhas e histórico de SRAG 2019–2024 — roda uma vez
01_coleta.py        baixa as fontes (só quando há extração nova)
02_tratamento.py    calcula os indicadores e gera docs/dados/*.json
03_painel.py        monta o site a partir de painel/template.html (só quando o visual muda)
04_validar.py       confere dados, privacidade e segurança antes de publicar
painel/             modelo do site e logo
docs/               o site publicado (fontes/: licença OFL; js/: D3, licença ISC; geo/: malha dos municípios)
data/ref/           tabelas de referência (versionadas)
.github/workflows/  a rotina automática
acionador/          acionador do Google que dispara a rotina a cada hora
```

Para rodar no computador:

```bash
pip install -r requirements.txt geopandas
python 00_referencias.py
python 01_coleta.py --arbo-semanas 60 --gdelt-horas 72   # precisa da chave em .mosqlimate_key ou MOSQLIMATE_KEY
python 02_tratamento.py
python 03_painel.py
python 04_validar.py
python -m http.server 8770 --directory docs
```

## Créditos

Desenvolvida pelo LADRI com o auxílio do Claude (Anthropic). Textos de interpretação revisados por profissionais da saúde da ASCES-UNITA. Contato: [@ladri_asces](https://www.instagram.com/ladri_asces/) · ladri.lapri.asces@gmail.com
