# UEX API 2.0: the fields this app reads

Checked against live responses on 2026-10-29. Raw samples are in `fixtures/samples/`; full responses go to `fixtures/raw/` (gitignored) via `python tools/explore_api.py`. Base URL `https://api.uexcorp.uk/2.0/`. Every endpoint below answered without a token; the app sends `Authorization: Bearer <UEX_TOKEN>` when `.env` has one.

## Endpoints used

| Endpoint | Call | Refresh | Notes |
|---|---|---|---|
| `commodities_prices_all` | 1 call, all terminals | every 5 min (config) | UEX caches it 30 min server-side |
| `fuel_prices_all` | 1 call | hourly | Quantum Fuel and Hydrogen Fuel rows |
| `terminals`, `commodities`, `vehicles`, `star_systems`, `commodities_status` | 1 call each | daily | |
| `space_stations`, `outposts`, `cities`, `poi` | 1 call each | daily | only source of pad sizes |
| `orbits_distances?id_star_system=N` | 1 per live system | daily | orbit-to-orbit, Gm |
| `terminals_distances?id_terminal_origin=A&id_terminal_destination=B` | 1 per pair | cached a day | gap fills and same-orbit hops only, paced ~20/min |

## Field map

| Need | Field | Reality |
|---|---|---|
| Buy price | `commodities_prices_all.price_buy` | aUEC per SCU. 0 = not sold there. |
| Sell price | `price_sell` | aUEC per SCU. 0 = not bought there. |
| Stock at buy | `scu_buy` (`scu_buy_avg`) | filled on 436 of 439 Stanton buy rows |
| Demand at sell | `scu_sell` ("scu sell demand (forecasted)"), then `scu_sell_avg` | filled on 137 / 339 of 1,195 Stanton sell rows. `scu_sell_stock` is the terminal's inventory, not demand. |
| Inventory level | `status_buy`, `status_sell` | codes from `commodities_status`: 1 Out of Stock, 2 Very Low, 3 Low, 4 Medium, 5 High, 6 Very High, 7 Maximum |
| Price age | `date_modified` | unix seconds, per price row |
| System | `terminals.id_star_system`, `star_system_name` | live: Stanton 68, Pyro 64, Nyx 55 |
| Terminal type | `terminals.type` | trade = `commodity`, `commodity_raw` |
| Orbit | `terminals.id_orbit`, `orbit_name` | joins to `orbits_distances.id_orbit_origin/destination` |
| Box sizes | `commodities_prices_all.container_sizes` (CSV), `terminals.max_container_size` | 0 / empty = unknown. `mcs` is deprecated. |
| Pad sizes | `pad_types` on `space_stations` / `outposts` / `cities` / `poi` | e.g. `"S,M,L"`; joined via `terminals.id_space_station` etc. Known for 18 of 128 Stanton trade terminals. |
| Docking | `terminals.has_docking_port`, `has_loading_dock`, `has_freight_elevator`, `is_auto_load` | shown, not used to filter |
| Live in game | `terminals.is_available_live` | 0 on 37 of 117 Stanton commodity terminals; excluded |
| Legality | `commodities.is_illegal` | 18 commodities, one global flag |
| Ship hold | `vehicles.scu`, `container_sizes`, `pad_type` | Centaurus id 292: 240, blank, L |
| Distance | `orbits_distances.distance`, `terminals_distances.distance` | strings, gigameters. `orbits_distances?id_star_system=N` also returns pairs from system N to the other systems (Stanton: 564 to Pyro, 174 to Nyx). |
| Jump points | gateway terminals, `place` like `Pyro Gateway (Stanton)` | no jump-point endpoint used; the gateway names say which systems connect |
| Fuel price | `fuel_prices_all.price_buy` where `commodity_name = "Quantum Fuel"` | median per system |

## SC Trade Tools community reports

`GET https://sc-trade.tools/api/crowdsource/commodity-listings?page=N`, documented at sc-trade.tools/swagger-ui, no token. 100 reports per page, newest first, cached by them for an hour. Their aggregated price endpoints need a paid token and carry no timestamps, so the app doesn't use them.

| Field | Meaning | Notes |
|---|---|---|
| `location` | `stanton > arccorp > baijini point` | last part matches a UEX terminal's place; gateways come without a system (`nyx gateway`) |
| `transaction` | `SELLS` (you buy) / `BUYS` (you sell) | |
| `commodity` | lower-case name | matches UEX `commodities.name` |
| `price` | aUEC per SCU | unfiltered: misread leading digits happen (54,400 for 3,700), so reports off by more than 2x from UEX are dropped |
| `quantity` | SCU in stock | used for the buy side only |
| `saturation` | 0 empty to 1 full | mapped to inventory status 1 to 7 |
| `boxSizesInScu` | list | often null |
| `timestamp` | ISO time in UTC, when the player saw it | newest report per terminal, commodity and side wins if it is newer than UEX |

## Not in the API (config instead)

- Quantum drive speed and spool: `qt_speed_gm_s`, `qt_spool_s` per ship.
- Fuel burn: `qt_fuel_scu_per_gm` per ship (`vehicles.fuel_quantum` is 0 for many ships).
- Max box for ships where `vehicles.container_sizes` is blank: `max_container_scu`.
- Time through a jump tunnel: `jump_min` under `[optimizer]`.

## Errors

Body `status` is `ok` on success. Seen or documented: `requests_limit_reached` (retried with backoff), `error` (HTTP 500), `missing_id_terminal_origin`, `missing_id_terminal_destination`, `missing_id_star_system`, `missing_required_input`, `missing_one_required_inputs`. Real bodies are in `fixtures/errors/`. Limit: 120 requests per minute (172,800 per day); the app caps itself at 60.
