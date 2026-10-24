<center align="center" style="text-align: center;justify-content:center;">
<div align="center" style="text-align: center;justify-content:center;">
<h1 align="center" style="text-align: center;justify-content:center;">

IMDb MCP server

<img style="justify-content:center;text-align: center;width: 95px; height: auto;" width="793" height="411" alt="image" src="https://github.com/user-attachments/assets/abed1a04-d69b-4ab4-a490-d606064df72d" />
<img style="justify-content:center;text-align: center;width: 94px; height: auto;" alt="IMDb" src="public/logo.png" />

</h1>


![Version](https://img.shields.io/badge/version-1.0.0-blue.svg?style=for-the-badge) ![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white) ![IMDb](https://img.shields.io/badge/IMDb-F5C518?style=for-the-badge&logo=imdb&logoColor=black) ![Tools](https://img.shields.io/badge/tools-21-brightgreen?style=for-the-badge)

</div>
</center>

<hr>

Read IMDb from Claude.ai, Claude Desktop and Claude Code: titles, people, credits, episodes, reviews, box office, charts and advanced search, and on top of that your own ratings and watchlist. "What did I rate Alien", "my 10s from the 1990s", "how do my scores compare to IMDb's" are one call each, over the whole history. No API key and no login: it talks to the same GraphQL API imdb.com itself uses, which answers public data to anyone.

<hr>

> [!WARNING]
> Using this server with a paid AI service costs money. Tool definitions and results are billed as input tokens, and an agent can call tools repeatedly on its own. You are responsible for every charge, so set spending limits with your provider. The author accepts no liability for any costs. See [DISCLAIMER.md](DISCLAIMER.md).

## Why not the other options

| Server | Source | Your ratings | Watchlist | Key needed |
| --- | --- | --- | --- | --- |
| [`uzaysozen/imdb-mcp-server`](https://github.com/uzaysozen/imdb-mcp-server) | RapidAPI, 5 results a page | no | no | RapidAPI subscription |
| [`clsung/mcp-imdb`](https://lobehub.com/mcp/clsung-mcp-imdb) | scraping | no | no | none |
| Cinemagoer based servers | scraping | no | no | none |
| This one | IMDb's own GraphQL API | **whole history** | **yes** | none |

None of them know who you are. This one reads the ratings and watchlist that your IMDb profile shows publicly, which is where the questions worth asking live.

## Tools

| Tool | What it does |
| --- | --- |
| `search_titles`, `search_names` | Find a film, series, episode or person and get its id |
| `get_title` | One title in full: plot, ratings, cast and crew, release, certificate, awards, poster |
| `get_title_credits`, `get_title_episodes`, `get_title_reviews`, `get_similar_titles`, `get_box_office` | The rest of a title page |
| `get_name`, `get_name_credits` | A person and their filmography |
| `advanced_title_search` | Genre, type, years, rating, votes, keywords and sort, like imdb.com/search/title |
| `get_chart` | Top 250, lowest rated, top TV, most popular |
| `get_coming_soon` | Upcoming films or series from a date |
| `my_ratings` | Your ratings a page at a time, newest or highest first |
| `my_rating_for` | What you gave one title, if anything |
| `find_my_ratings` | Search the whole history: words, years, rating range, genre, type, date rated |
| `my_rating_stats` | Count, average, spread from 1 to 10, per year, decades, genres, difference to IMDb's averages |
| `my_watchlist`, `get_list` | Your watchlist, or any public list by its `ls` id |
| `get_user_profile` | Who the server is reading, with ratings and watchlist counts |
| `get_result_page` | The rest of an answer too large to send at once |

The ratings history is fetched once, 250 at a time, and kept for 15 minutes, so a run of questions about it costs one round of requests.

## Setup

```bash
git clone https://github.com/rollecode/imdb-mcp.git
cd imdb-mcp
uv venv && uv pip install -e .
```

Tell it who you are. Open your ratings page on imdb.com, `https://www.imdb.com/user/p.xxxx/ratings/`, and copy the `p.xxxx` part:

```bash
export IMDB_PROFILE_ID=p.xxxx
```

`IMDB_USER_ID=ur12345678` works too if you know it. A `.env` in the working directory works as well. Your ratings and watchlist must be set public in your IMDb settings, since the server has no login; everything else needs nothing.

### Claude Code

```bash
claude mcp add imdb -- /path/to/imdb-mcp/.venv/bin/imdb-mcp
```

## Hosting it

Running it over HTTP puts it in reach of Claude.ai and Claude Desktop as a custom connector, and of Claude Code on other machines. Three tiers, the same shape the other servers in this family use:

| Tier | Port | What it does |
| --- | --- | --- |
| `imdb-mcp` | 8620 | The server. No login of its own, never exposed |
| nginx | 8621 | Front door, behind a Cloudflare Tunnel |
| `auth-server.js` | 8622 | OAuth 2.1 sign-in, or a fixed bearer token |

```bash
npm install
node set-password.js 'a password for the sign-in page'
printf 'IMDB_PROFILE_ID=p.xxxx\n' > ~/.config/imdb-mcp/env
chmod 600 ~/.config/imdb-mcp/env
```

Copy `systemd/*.service` to `/etc/systemd/system/`, replacing `YOUR_USER` and the `ISSUER`, and `systemd/imdb-mcp.nginx.conf` to your nginx sites. Point a tunnel hostname at `127.0.0.1:8621`. In Claude.ai: Settings, Connectors, Add custom connector, the URL `https://your-host/mcp`, client id and secret blank, then sign in with the password.

A fixed token in `~/.config/imdb-mcp/token` is accepted as a bearer token too:

```bash
claude mcp add --transport http imdb https://your-host/mcp --header "Authorization: Bearer $(cat ~/.config/imdb-mcp/token)"
```

## IMDb's terms

The API answers with a notice: public, commercial or non-private use of its data is not allowed, and limited non-commercial use is governed by [IMDb's conditions](https://help.imdb.com/article/imdb/general-information/can-i-use-imdb-data-in-my-software/G5JTRESSHJBBHTGX). This server is for reading your own account and looking things up for yourself. Do not put it in front of other people or a product, and do not store or republish what it returns.

Information courtesy of IMDb (https://www.imdb.com). Used with permission.

## Development

```bash
uv pip install -e . --group dev
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

The tests run every tool against a fake IMDb and check the GraphQL each one builds, the shape it returns, and that the ratings history is fetched once.
