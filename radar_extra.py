#!/usr/bin/env python3
"""Radar de Voos - cliente extra (SerpApi / Google Flights).

Consulta poucas combinacoes de datas por execucao (a cota gratuita e de 250/mes e e
compartilhada com o cliente da universidade), guarda TODO o historico em historico.csv
e gera precos-extra.json no mesmo formato do precos-automaticos.json lido pela pagina.

Uso:  python radar_extra.py            (uma rodada normal)
      python radar_extra.py --simular  (mostra o que consultaria, sem gastar cota)
      python radar_extra.py --saldo    (so mostra o saldo da conta, nao gasta cota)
Somente biblioteca padrao do Python 3.9+.
"""
import csv, datetime as dt, json, os, sys, urllib.parse, urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
P = lambda nome: os.path.join(AQUI, nome)
UTC = dt.timezone.utc


def ler_env():
    env = {}
    try:
        with open(P('.env'), encoding='utf-8') as f:
            for linha in f:
                linha = linha.strip()
                if linha and not linha.startswith('#') and '=' in linha:
                    k, v = linha.split('=', 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    env.update({k: v for k, v in os.environ.items() if k in ('SERPAPI_KEY', 'LIMITE_MENSAL', 'POR_RODADA', 'RESERVA')})
    return env


def ler_json(nome, padrao):
    try:
        with open(P(nome), encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return padrao


def gravar_json(nome, dados):
    tmp = P(nome + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    os.replace(tmp, P(nome))


def http_json(url, params):
    req = urllib.request.Request(url + '?' + urllib.parse.urlencode(params), headers={'User-Agent': 'radar-de-voos/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {'error': f'HTTP {e.code}'}


def saldo(chave):
    """Consulta a conta (nao gasta cota)."""
    d = http_json('https://serpapi.com/account.json', {'api_key': chave})
    if 'error' in d:
        raise SystemExit('SerpApi recusou a chave: ' + d['error'])
    return d.get('total_searches_left', d.get('plan_searches_left'))


def candidatos(rota, hoje):
    ini = dt.date.fromisoformat(rota['inicio'])
    fim = dt.date.fromisoformat(rota['fim'])
    passo = max(1, int(rota.get('passo', 1)))
    d = max(ini, hoje + dt.timedelta(days=2))
    if d > ini and passo > 1:  # mantem o alinhamento da grade de datas
        d = ini + dt.timedelta(days=-(-(d - ini).days // passo) * passo)
    while d <= fim:
        for n in range(rota['min'], rota['max'] + 1):
            yield d.isoformat(), (d + dt.timedelta(days=n)).isoformat()
        d += dt.timedelta(days=passo)


def escolher(rotas, estado, quantos, hoje, agora):
    """Escolhe as combinacoes ha mais tempo sem consulta, ponderadas pela prioridade da rota."""
    ultimo = estado.setdefault('ultimo', {})
    opcoes = []
    for r in rotas:
        for ida, volta in candidatos(r, hoje):
            chave = f"{r['id']}|{ida}|{volta}"
            visto = ultimo.get(chave)
            horas = 10000 if not visto else (agora - dt.datetime.fromisoformat(visto)).total_seconds() / 3600
            if horas < 20:  # nunca repete a mesma combinacao no mesmo dia
                continue
            proximidade = 1 + 1 / (1 + (dt.date.fromisoformat(ida) - hoje).days / 30)  # datas proximas mudam mais
            if r.get('preferida'):  # data desejada vem primeiro; vizinhas logo depois
                proximidade *= 1 + 2 / (1 + abs((dt.date.fromisoformat(ida) - dt.date.fromisoformat(r['preferida'])).days))
            opcoes.append([horas * r.get('peso', 1) * proximidade, r, ida, volta, chave])
    escolhidas, usadas = [], {}
    while opcoes and len(escolhidas) < quantos:
        for o in opcoes:  # espalha entre as rotas
            o[0] /= 1 + usadas.get(o[1]['id'], 0)
        opcoes.sort(key=lambda o: -o[0])
        o = opcoes.pop(0)
        escolhidas.append(o)
        usadas[o[1]['id']] = usadas.get(o[1]['id'], 0) + 1
    return escolhidas


def consultar(chave, rota, ida, volta):
    d = http_json('https://serpapi.com/search.json', {
        'engine': 'google_flights', 'api_key': chave, 'type': 1,
        'departure_id': ','.join(rota['origens']), 'arrival_id': ','.join(rota['destinos']),
        'outbound_date': ida, 'return_date': volta,
        'currency': 'BRL', 'hl': 'pt', 'gl': 'br', 'adults': 1, 'travel_class': 1,
    })
    if 'error' in d:
        if 'no results' in d['error'].lower():
            return [], {}, None
        raise RuntimeError(d['error'])
    melhores = {}
    for grupo in ('best_flights', 'other_flights'):
        for it in d.get(grupo) or []:
            trechos = it.get('flights') or []
            if not trechos or not isinstance(it.get('price'), (int, float)):
                continue
            org = trechos[0]['departure_airport']['id']
            dst = trechos[-1]['arrival_airport']['id']
            cias = sorted({t.get('airline', '') for t in trechos if t.get('airline')})
            oferta = {'origem': org, 'destino': dst, 'tarifa': float(it['price']), 'ida': ida, 'volta': volta,
                      'cia': ' + '.join(cias), 'paradas': len(trechos) - 1,
                      'duracao_min': it.get('total_duration')}
            k = (org, dst)
            if k not in melhores or oferta['tarifa'] < melhores[k]['tarifa']:
                melhores[k] = oferta
    pi = d.get('price_insights') or {}
    return list(melhores.values()), pi, (d.get('search_metadata') or {}).get('google_flights_url')


CAMPOS = ['consultado', 'rota', 'origem', 'destino', 'ida', 'volta', 'tarifa', 'cia', 'paradas',
          'duracao_min', 'nivel_preco', 'tipico_min', 'tipico_max']


def main():
    env = ler_env()
    chave = env.get('SERPAPI_KEY', '')
    if not chave or 'cole_sua_chave' in chave:
        raise SystemExit('Coloque a chave da SerpApi no arquivo .env (SERPAPI_KEY=...).')
    rotas = ler_json('rotas.json', {})['rotas']
    estado = ler_json('estado.json', {})
    agora = dt.datetime.now(UTC).replace(microsecond=0)
    hoje = agora.date()
    mes = agora.strftime('%Y-%m')
    if estado.get('mes') != mes:
        estado['mes'], estado['gastas_mes'] = mes, 0

    restante = saldo(chave)
    print(f'Saldo da conta SerpApi: {restante} consultas.')
    if '--saldo' in sys.argv:
        return
    limite = int(env.get('LIMITE_MENSAL', 120))
    por_rodada = int(env.get('POR_RODADA', 3))
    reserva = int(env.get('RESERVA', 15))  # deixa folga para o cliente da universidade
    quantos = max(0, min(por_rodada, limite - estado['gastas_mes'], (restante or 0) - reserva))
    escolhidas = escolher(rotas, estado, quantos, hoje, agora)
    if '--simular' in sys.argv:
        for _, r, ida, volta, _ in escolhidas:
            print(f"consultaria {r['id']}: {','.join(r['origens'])} -> {','.join(r['destinos'])} {ida} a {volta}")
        return

    novo_csv = not os.path.exists(P('historico.csv'))
    falhas = 0
    with open(P('historico.csv'), 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if novo_csv:
            w.writeheader()
        for _, r, ida, volta, k in escolhidas:
            try:
                ofertas, pi, link = consultar(chave, r, ida, volta)
            except Exception as e:  # rede, cota, etc.: segue com as outras
                falhas += 1
                print(f"FALHA {r['id']} {ida}: {e}")
                continue
            estado['gastas_mes'] += 1
            estado['ultimo'][k] = agora.isoformat()
            faixa = pi.get('typical_price_range') or [None, None]
            for o in ofertas:
                w.writerow({'consultado': agora.isoformat(), 'rota': r['id'], **{c: o.get(c) for c in CAMPOS[2:10]},
                            'nivel_preco': pi.get('price_level'), 'tipico_min': faixa[0], 'tipico_max': faixa[-1]})
            if not ofertas:
                w.writerow({'consultado': agora.isoformat(), 'rota': r['id'], 'ida': ida, 'volta': volta})
            resumo = ', '.join(f"{o['origem']} R$ {o['tarifa']:.0f}" for o in sorted(ofertas, key=lambda o: o['tarifa'])) or 'sem voos'
            print(f"{r['id']} {ida} a {volta}: {resumo}")

    gastas = len(escolhidas) - falhas
    publicar(rotas, estado, agora, falhas, (restante or 0) - gastas)
    gravar_json('estado.json', estado)


def publicar(rotas, estado, agora, falhas, saldo_final):
    """Monta precos-extra.json com a observacao mais recente de cada (rota, origem, destino, ida, volta)."""
    hoje = agora.date().isoformat()
    ultimas = {}
    serie = {}
    try:
        with open(P('historico.csv'), encoding='utf-8') as f:
            for l in csv.DictReader(f):
                if not l.get('tarifa'):
                    continue
                k = (l['rota'], l['origem'], l['destino'], l['ida'], l['volta'])
                ultimas[k] = l  # o CSV esta em ordem cronologica
                dia = l['consultado'][:10]
                s = serie.setdefault(l['rota'], {})
                s[dia] = min(s.get(dia, 1e12), float(l['tarifa']))
    except FileNotFoundError:
        pass
    validas_rotas = {r['id'] for r in rotas}
    ofertas = []
    for (rid, org, dst, ida, volta), l in ultimas.items():
        if rid not in validas_rotas or ida <= hoje:
            continue
        o = {'origem': org, 'destino': dst, 'tarifa': float(l['tarifa']), 'ida': ida, 'volta': volta,
             'consultado': l['consultado'], 'rota': rid, 'cia': l.get('cia') or '',
             'paradas': int(l['paradas']) if (l.get('paradas') or '').isdigit() else None}
        if l.get('nivel_preco'):
            o['nivel'] = l['nivel_preco']
        ofertas.append(o)
    ofertas.sort(key=lambda o: o['tarifa'])

    anterior = ler_json('precos-extra.json', {})
    min_ant = {}
    for r in rotas:
        prev = [o['tarifa'] for o in anterior.get('ofertas', []) if o.get('rota') == r['id']]
        if prev:
            min_ant[r['id']] = min(prev)
    rotas_pub = []
    for r in rotas:
        pares = sum(1 for _ in candidatos(r, agora.date()))
        recentes = sum(1 for k, v in estado.get('ultimo', {}).items() if k.startswith(r['id'] + '|')
                       and (agora - dt.datetime.fromisoformat(v)).days < 15)
        rotas_pub.append({k: r[k] for k in ('id', 'nome', 'origens', 'destinos', 'inicio', 'fim', 'min', 'max', 'teto')}
                         | {'cobertura': {'pares': pares, 'com_preco_recente': min(recentes, pares)}})
    gravar_json('precos-extra.json', {
        'versao': 1, 'moeda': 'BRL', 'fonte': 'extra', 'atualizado': agora.isoformat(),
        'rotas': rotas_pub, 'ofertas': ofertas, 'falhas': falhas, 'anteriorMinimo': min_ant, 'saldo': saldo_final,
        'serie': {rid: sorted(d.items()) for rid, d in serie.items()},
    })
    print(f'precos-extra.json: {len(ofertas)} ofertas em {len(rotas_pub)} rotas.')


if __name__ == '__main__':
    main()
