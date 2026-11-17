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


JANELA_USO = 14  # dias usados para dividir a cota entre as rotas


def uso_recente(estado, rid, agora):
    """Quantas consultas a rota recebeu nos ultimos JANELA_USO dias."""
    lim = agora - dt.timedelta(days=JANELA_USO)
    return sum(1 for t in estado.get('consultas', {}).get(rid, []) if dt.datetime.fromisoformat(t) >= lim)


def registrar_consulta(estado, rid, agora):
    lim = agora - dt.timedelta(days=JANELA_USO)
    lst = [t for t in estado.setdefault('consultas', {}).get(rid, []) if dt.datetime.fromisoformat(t) >= lim]
    estado['consultas'][rid] = lst + [agora.isoformat()]


def escolher(rotas, estado, quantos, hoje, agora):
    """1) Divide a cota entre as rotas na proporcao do 'peso' (a rota com menos consultas recentes
    por unidade de peso e a proxima a ser atendida); 2) dentro da rota, pega a combinacao de datas
    nunca vista ou ha mais tempo sem consulta, preferindo datas proximas e a data 'preferida'."""
    ultimo = estado.setdefault('ultimo', {})
    if 'consultas' not in estado:  # estado antigo: reconstroi o uso a partir das ultimas consultas
        estado['consultas'] = {}
        for k, v in ultimo.items():
            estado['consultas'].setdefault(k.split('|')[0], []).append(v)
    por_rota = {}
    for r in rotas:
        ops = []
        for ida, volta in candidatos(r, hoje):
            chave = f"{r['id']}|{ida}|{volta}"
            visto = ultimo.get(chave)
            horas = None if not visto else (agora - dt.datetime.fromisoformat(visto)).total_seconds() / 3600
            if horas is not None and horas < 20:  # nunca repete a mesma combinacao no mesmo dia
                continue
            proximidade = 1 + 1 / (1 + (dt.date.fromisoformat(ida) - hoje).days / 30)  # datas proximas mudam mais
            if r.get('preferida'):  # data desejada vem primeiro; vizinhas logo depois
                proximidade *= 1 + 2 / (1 + abs((dt.date.fromisoformat(ida) - dt.date.fromisoformat(r['preferida'])).days))
            # nunca consultada vem antes de qualquer outra; depois, a mais antiga (ponderada pela proximidade)
            ops.append([(horas is None, (horas or 0) * proximidade, proximidade), r, ida, volta, chave])
        if ops:
            ops.sort(key=lambda o: o[0], reverse=True)
            por_rota[r['id']] = ops
    uso = {rid: uso_recente(estado, rid, agora) for rid in por_rota}
    peso = {r['id']: max(0.1, float(r.get('peso', 1))) for r in rotas}
    escolhidas = []
    while por_rota and len(escolhidas) < quantos:
        rid = min(por_rota, key=lambda i: ((uso[i] + 0.5) / peso[i], -peso[i]))
        escolhidas.append(por_rota[rid].pop(0))
        uso[rid] += 1
        if not por_rota[rid]:
            del por_rota[rid]
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
          'duracao_min', 'nivel_preco', 'tipico_min', 'tipico_max', 'fonte']
UNI_URL = 'https://docs.ufpr.br/~jefer/precos-automaticos.json'


def migrar_csv():
    """Acrescenta a coluna 'fonte' em historicos antigos."""
    if not os.path.exists(P('historico.csv')):
        return
    with open(P('historico.csv'), encoding='utf-8') as f:
        leitor = csv.DictReader(f)
        linhas = list(leitor)
        cab = leitor.fieldnames or []
    if cab and 'fonte' not in cab:
        with open(P('historico.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS)
            w.writeheader()
            for l in linhas:
                w.writerow({**{c: l.get(c) for c in CAMPOS}, 'fonte': 'extra'})


def importar_universidade(w, vistos):
    """Copia para o historico os precos publicados pelo cliente da universidade (sem gastar cota)."""
    try:
        req = urllib.request.Request(UNI_URL, headers={'User-Agent': 'radar-de-voos/1.0'})
        with urllib.request.urlopen(req, timeout=60) as r:
            uni = json.load(r)
    except Exception as e:
        print('Aviso: nao consegui ler o JSON da universidade:', e)
        return None
    novos = 0
    for o in uni.get('ofertas', []):
        k = (o.get('consultado', ''), o.get('rota', ''), o.get('origem'), o.get('destino'), o.get('ida'), o.get('volta'))
        if k in vistos or not o.get('consultado'):
            continue
        vistos.add(k)
        w.writerow({'consultado': o['consultado'], 'rota': o.get('rota', ''), 'origem': o['origem'], 'destino': o['destino'],
                    'ida': o['ida'], 'volta': o['volta'], 'tarifa': o['tarifa'], 'fonte': 'universidade'})
        novos += 1
    print(f'Universidade: {novos} precos novos copiados para o historico.')
    return uni


def main():
    env = ler_env()
    chave = env.get('SERPAPI_KEY', '')
    if not chave or 'cole_sua_chave' in chave:
        raise SystemExit('Coloque a chave da SerpApi no arquivo .env (SERPAPI_KEY=...).')
    rotas = ler_json('rotas.json', {})['rotas']
    estado = ler_json('estado.json', {})
    estado.setdefault('ultimo', {})
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

    migrar_csv()
    vistos = set()
    if os.path.exists(P('historico.csv')):
        with open(P('historico.csv'), encoding='utf-8') as f:
            for l in csv.DictReader(f):
                vistos.add((l['consultado'], l['rota'], l['origem'], l['destino'], l['ida'], l['volta']))
    novo_csv = not os.path.exists(P('historico.csv'))
    falhas = 0
    with open(P('historico.csv'), 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if novo_csv:
            w.writeheader()
        uni = importar_universidade(w, vistos)
        for _, r, ida, volta, k in escolhidas:
            try:
                ofertas, pi, link = consultar(chave, r, ida, volta)
            except Exception as e:  # rede, cota, etc.: segue com as outras
                falhas += 1
                print(f"FALHA {r['id']} {ida}: {e}")
                continue
            estado['gastas_mes'] += 1
            estado['ultimo'][k] = agora.isoformat()
            registrar_consulta(estado, r['id'], agora)
            faixa = pi.get('typical_price_range') or [None, None]
            for o in ofertas:
                w.writerow({'consultado': agora.isoformat(), 'rota': r['id'], **{c: o.get(c) for c in CAMPOS[2:10]},
                            'nivel_preco': pi.get('price_level'), 'tipico_min': faixa[0], 'tipico_max': faixa[-1],
                            'fonte': 'extra'})
            if not ofertas:
                w.writerow({'consultado': agora.isoformat(), 'rota': r['id'], 'ida': ida, 'volta': volta, 'fonte': 'extra'})
            resumo = ', '.join(f"{o['origem']} R$ {o['tarifa']:.0f}" for o in sorted(ofertas, key=lambda o: o['tarifa'])) or 'sem voos'
            print(f"{r['id']} {ida} a {volta}: {resumo}")

    gastas = len(escolhidas) - falhas
    publicar(rotas, estado, agora, falhas, (restante or 0) - gastas, uni)
    gravar_json('estado.json', estado)


def ler_historico():
    try:
        with open(P('historico.csv'), encoding='utf-8') as f:
            linhas = [l for l in csv.DictReader(f) if l.get('tarifa')]
    except FileNotFoundError:
        return []
    linhas.sort(key=lambda l: l['consultado'])
    return linhas


def melhor_por_dia(linhas, validade=21):
    """Para cada rota e cada dia: o menor preco conhecido naquele dia, usando a observacao mais
    recente de cada combinacao de datas (vista nos ultimos `validade` dias e com ida ainda futura)."""
    por_rota = {}
    for l in linhas:
        por_rota.setdefault(l['rota'], []).append(l)
    serie = {}
    for rid, ls in por_rota.items():
        atual, pos, s = {}, 0, []
        for d in sorted({l['consultado'][:10] for l in ls}):
            while pos < len(ls) and ls[pos]['consultado'][:10] <= d:
                l = ls[pos]
                atual[(l['origem'], l['destino'], l['ida'], l['volta'])] = l
                pos += 1
            lim = (dt.date.fromisoformat(d) - dt.timedelta(days=validade)).isoformat()
            vivos = [l for l in atual.values() if l['ida'] > d and l['consultado'][:10] >= lim]
            if vivos:
                b = min(vivos, key=lambda l: float(l['tarifa']))
                s.append({'dia': d, 'preco': float(b['tarifa']), 'origem': b['origem'], 'destino': b['destino'],
                          'ida': b['ida'], 'volta': b['volta']})
        serie[rid] = s
    return serie


def brl(v):
    return 'R$ ' + f'{v:,.0f}'.replace(',', '.')


def gerar_alertas(serie, tetos, nomes, agora):
    """Registra (sem repetir) alertas do dia: entrou no orcamento, queda de preco e menor preco ja visto."""
    alertas = ler_json('alertas.json', [])
    ja = {(a['dia'], a['rota'], a['tipo']) for a in alertas}
    for rid, serie_rota in serie.items():
      for i in range(len(serie_rota)):  # percorre todos os dias: alertas antigos tambem entram (sem repetir)
        s = serie_rota[:i + 1]
        b = s[-1]
        hoje = b['dia']
        ant = s[-2] if len(s) > 1 else None
        nome = nomes.get(rid, rid)
        onde = f"saindo de {b['origem']}, {b['ida'][8:10]}/{b['ida'][5:7]} a {b['volta'][8:10]}/{b['volta'][5:7]}"
        novos = []
        teto = tetos.get(rid)
        if teto and b['preco'] <= teto and (not ant or ant['preco'] > teto):
            novos.append(('orcamento', f"{nome}: {brl(b['preco'])}, dentro do seu limite de {brl(teto)} ({onde})."))
        if ant and b['preco'] <= ant['preco'] * 0.97:
            pct = (1 - b['preco'] / ant['preco']) * 100
            novos.append(('queda', f"{nome}: caiu {brl(ant['preco'] - b['preco'])} ({pct:.0f}%), agora {brl(b['preco'])} ({onde})."))
        anteriores = [x['preco'] for x in s[:-1]]
        if len(anteriores) >= 3 and b['preco'] < min(anteriores):
            novos.append(('recorde', f"{nome}: {brl(b['preco'])} é o menor preço já registrado ({onde})."))
        for tipo, msg in novos:
            if (hoje, rid, tipo) not in ja:
                ja.add((hoje, rid, tipo))
                alertas.append({'quando': agora.isoformat() if hoje == agora.date().isoformat() else hoje + 'T12:00:00+00:00', 'dia': hoje, 'rota': rid, 'tipo': tipo,
                                'mensagem': msg, 'preco': b['preco']})
                print('ALERTA', msg)
    alertas = sorted(alertas, key=lambda a: a['dia'])[-300:]
    gravar_json('alertas.json', alertas)
    return alertas


def publicar(rotas, estado, agora, falhas, saldo_final, uni=None):
    """Monta precos-extra.json: ofertas atuais deste cliente + historico diario e alertas de TODOS os destinos."""
    hoje = agora.date().isoformat()
    linhas = ler_historico()
    ultimas = {}
    for l in linhas:
        if (l.get('fonte') or 'extra') == 'extra':
            ultimas[(l['rota'], l['origem'], l['destino'], l['ida'], l['volta'])] = l
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

    serie = melhor_por_dia(linhas)
    tetos = {r['id']: r.get('teto') for r in rotas}
    nomes = {r['id']: r['nome'] for r in rotas}
    for r in (uni or {}).get('rotas', []):
        tetos.setdefault(r['id'], r.get('teto'))
        nomes.setdefault(r['id'], r.get('nome', r['id']))
    alertas = gerar_alertas(serie, tetos, nomes, agora)
    proxima = agora.replace(hour=12, minute=15, second=0)
    if proxima <= agora:
        proxima += dt.timedelta(days=1)
    gravar_json('precos-extra.json', {
        'versao': 1, 'moeda': 'BRL', 'fonte': 'extra', 'atualizado': agora.isoformat(),
        'rotas': rotas_pub, 'ofertas': ofertas, 'falhas': falhas, 'anteriorMinimo': min_ant, 'saldo': saldo_final,
        'serie': serie, 'alertas': alertas[-60:], 'proxima': proxima.isoformat(),
    })
    print(f'precos-extra.json: {len(ofertas)} ofertas, historico de {len(serie)} destinos, {len(alertas)} alertas.')


if __name__ == '__main__':
    main()
