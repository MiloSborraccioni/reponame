#!/usr/bin/env python3
"""Genera il report di compravendita titoli da ``registro_operazioni.csv``.

Uso:
    python3 genera_report.py                 # esercizio corrente, costo medio ponderato
    python3 genera_report.py --anno 2026     # esercizio specifico
    python3 genera_report.py --metodo fifo   # metodo di scarico: medio | fifo | lifo

Produce in ``report/``:
    report_<anno>.md          report leggibile da consegnare al commercialista
    plusvalenze_<anno>.csv    una riga per ogni vendita con plus/minusvalenza
    posizioni_aperte.csv      titoli ancora in portafoglio (solo se presenti)

Solo libreria standard: nessuna dipendenza da installare.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTRO = ROOT / "registro_operazioni.csv"
REPORT_DIR = ROOT / "report"

CENT = Decimal("0.01")
PREZZO = Decimal("0.0001")

CAMPI = [
    "data", "tipo", "titolo", "isin", "mercato", "quantita", "prezzo",
    "controvalore_lordo", "commissioni", "n_ordine", "conto_titoli", "note",
]


@dataclass
class Operazione:
    data: date
    tipo: str  # "Acquisto" | "Vendita"
    titolo: str
    isin: str
    mercato: str
    quantita: Decimal
    prezzo: Decimal
    controvalore_lordo: Decimal
    commissioni: Decimal
    n_ordine: str
    conto_titoli: str
    note: str

    @property
    def esborso_o_incasso_netto(self) -> Decimal:
        """Acquisto: quanto e' uscito dal conto. Vendita: quanto e' entrato."""
        if self.tipo == "Acquisto":
            return self.controvalore_lordo + self.commissioni
        return self.controvalore_lordo - self.commissioni


@dataclass
class Lotto:
    data: date
    quantita: Decimal
    costo_totale: Decimal  # controvalore + commissioni di acquisto

    @property
    def costo_unitario(self) -> Decimal:
        return self.costo_totale / self.quantita


@dataclass
class Realizzo:
    vendita: Operazione
    costo_fiscale: Decimal
    lotti_scaricati: list[tuple[date, Decimal, Decimal]]  # (data acq., qta, costo)

    @property
    def incasso_netto(self) -> Decimal:
        return self.vendita.esborso_o_incasso_netto

    @property
    def risultato(self) -> Decimal:
        return self.incasso_netto - self.costo_fiscale

    @property
    def rendimento_pct(self) -> Decimal:
        if self.costo_fiscale == 0:
            return Decimal(0)
        return self.risultato / self.costo_fiscale * 100

    @property
    def giorni_detenzione(self) -> int:
        prima = min(d for d, _, _ in self.lotti_scaricati)
        return (self.vendita.data - prima).days


@dataclass
class Posizione:
    titolo: str
    isin: str
    lotti: list[Lotto] = field(default_factory=list)

    @property
    def quantita(self) -> Decimal:
        return sum((l.quantita for l in self.lotti), Decimal(0))

    @property
    def costo_totale(self) -> Decimal:
        return sum((l.costo_totale for l in self.lotti), Decimal(0))


# ----------------------------------------------------------------- lettura CSV

def _dec(valore: str, campo: str, riga: int) -> Decimal:
    valore = (valore or "").strip().replace(",", ".")
    if valore == "":
        return Decimal(0)
    try:
        return Decimal(valore)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Riga {riga}: valore non numerico per '{campo}': {valore!r}") from exc


def leggi_registro(percorso: Path) -> list[Operazione]:
    if not percorso.exists():
        raise SystemExit(f"Registro non trovato: {percorso}")
    operazioni: list[Operazione] = []
    with percorso.open(newline="", encoding="utf-8") as fh:
        lettore = csv.DictReader(fh, delimiter=";")
        mancanti = [c for c in CAMPI if c not in (lettore.fieldnames or [])]
        if mancanti:
            raise SystemExit(f"Colonne mancanti nel registro: {', '.join(mancanti)}")
        for n, r in enumerate(lettore, start=2):
            if not any((v or "").strip() for v in r.values()):
                continue
            tipo = (r["tipo"] or "").strip().capitalize()
            if tipo not in ("Acquisto", "Vendita"):
                raise SystemExit(f"Riga {n}: tipo deve essere Acquisto o Vendita, trovato {r['tipo']!r}")
            try:
                giorno = datetime.strptime(r["data"].strip(), "%Y-%m-%d").date()
            except ValueError as exc:
                raise SystemExit(f"Riga {n}: data non valida {r['data']!r} (atteso AAAA-MM-GG)") from exc
            qta = _dec(r["quantita"], "quantita", n)
            prezzo = _dec(r["prezzo"], "prezzo", n)
            lordo = _dec(r["controvalore_lordo"], "controvalore_lordo", n)
            if lordo == 0 and qta and prezzo:
                lordo = (qta * prezzo).quantize(CENT, ROUND_HALF_UP)
            if prezzo == 0 and qta:
                prezzo = (lordo / qta).quantize(PREZZO, ROUND_HALF_UP)
            operazioni.append(
                Operazione(
                    data=giorno,
                    tipo=tipo,
                    titolo=(r["titolo"] or "").strip(),
                    isin=(r["isin"] or "").strip().upper(),
                    mercato=(r["mercato"] or "").strip(),
                    quantita=qta,
                    prezzo=prezzo,
                    controvalore_lordo=lordo,
                    commissioni=_dec(r["commissioni"], "commissioni", n),
                    n_ordine=(r["n_ordine"] or "").strip(),
                    conto_titoli=(r["conto_titoli"] or "").strip(),
                    note=(r["note"] or "").strip(),
                )
            )
    # A parita' di data gli acquisti precedono le vendite.
    operazioni.sort(key=lambda o: (o.data, 0 if o.tipo == "Acquisto" else 1))
    return operazioni


# ------------------------------------------------------------------- calcolo

def elabora(operazioni: list[Operazione], metodo: str):
    posizioni: dict[str, Posizione] = {}
    realizzi: list[Realizzo] = []
    avvisi: list[str] = []

    for op in operazioni:
        pos = posizioni.setdefault(op.isin, Posizione(op.titolo, op.isin))
        if op.tipo == "Acquisto":
            pos.lotti.append(Lotto(op.data, op.quantita, op.controvalore_lordo + op.commissioni))
            continue

        if pos.quantita < op.quantita:
            avvisi.append(
                f"{op.data:%d/%m/%Y} vendita di {op.quantita} {op.titolo} ({op.isin}) "
                f"ma in carico risultano solo {pos.quantita}: verificare il registro."
            )

        residuo = op.quantita
        scaricati: list[tuple[date, Decimal, Decimal]] = []
        if metodo == "medio":
            if pos.quantita > 0:
                costo_medio = pos.costo_totale / pos.quantita
                qta = min(residuo, pos.quantita)
                costo = costo_medio * qta
                scaricati.append((min(l.data for l in pos.lotti), qta, costo))
                # Il pool mantiene il costo medio: si riducono i lotti in proporzione.
                fattore = (pos.quantita - qta) / pos.quantita
                for l in pos.lotti:
                    l.costo_totale *= fattore
                    l.quantita *= fattore
                pos.lotti = [l for l in pos.lotti if l.quantita > 0]
                residuo -= qta
        else:
            ordine = pos.lotti if metodo == "fifo" else list(reversed(pos.lotti))
            for l in ordine:
                if residuo <= 0:
                    break
                qta = min(residuo, l.quantita)
                costo = l.costo_unitario * qta
                scaricati.append((l.data, qta, costo))
                l.costo_totale -= costo
                l.quantita -= qta
                residuo -= qta
            pos.lotti = [l for l in pos.lotti if l.quantita > 0]

        if residuo > 0:
            # Quantita' venduta senza carico: costo zero, viene segnalato negli avvisi.
            scaricati.append((op.data, residuo, Decimal(0)))

        costo_fiscale = sum((c for _, _, c in scaricati), Decimal(0))
        realizzi.append(Realizzo(op, costo_fiscale, scaricati))

    aperte = [p for p in posizioni.values() if p.quantita > 0]
    return realizzi, aperte, avvisi


# ---------------------------------------------------------------- formattazione

def it(valore: Decimal | int, decimali: int = 2) -> str:
    """Formato italiano: 4.986,59"""
    q = Decimal(valore).quantize(Decimal(1).scaleb(-decimali), ROUND_HALF_UP)
    intero, _, dec = f"{q:,.{decimali}f}".partition(".")
    intero = intero.replace(",", ".")
    return f"{intero},{dec}" if decimali else intero


def eur(valore: Decimal) -> str:
    return f"€ {it(valore)}"


def qta(valore: Decimal) -> str:
    return it(valore, 0) if valore == valore.to_integral_value() else it(valore, 4)


def data_it(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def tabella(intestazioni: list[str], righe: list[list[str]], allinea_dx: set[int] = frozenset()) -> str:
    sep = ["---:" if i in allinea_dx else "---" for i in range(len(intestazioni))]
    out = ["| " + " | ".join(intestazioni) + " |", "| " + " | ".join(sep) + " |"]
    out += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in righe]
    return "\n".join(out)


# ------------------------------------------------------------------- report

METODI = {
    "medio": "costo medio ponderato",
    "fifo": "FIFO (primo entrato, primo uscito)",
    "lifo": "LIFO (ultimo entrato, primo uscito)",
}


def genera_markdown(anno: int, metodo: str, ops: list[Operazione], realizzi: list[Realizzo],
                    aperte: list[Posizione], avvisi: list[str], intestatario: str) -> str:
    acquisti = [o for o in ops if o.tipo == "Acquisto"]
    vendite = [o for o in ops if o.tipo == "Vendita"]
    tot_acq = sum((o.esborso_o_incasso_netto for o in acquisti), Decimal(0))
    tot_ven = sum((o.esborso_o_incasso_netto for o in vendite), Decimal(0))
    tot_comm = sum((o.commissioni for o in ops), Decimal(0))
    plus = sum((r.risultato for r in realizzi if r.risultato > 0), Decimal(0))
    minus = sum((r.risultato for r in realizzi if r.risultato < 0), Decimal(0))
    conti = sorted({o.conto_titoli for o in ops if o.conto_titoli})

    L: list[str] = []
    L.append(f"# Registro compravendita titoli — esercizio {anno}")
    L.append("")
    L.append(f"- **Intestatario:** {intestatario}")
    if conti:
        L.append(f"- **Conto titoli:** {', '.join(conti)}")
    L.append(f"- **Metodo di determinazione del costo:** {METODI[metodo]}")
    L.append(f"- **Generato il:** {data_it(date.today())} da `registro_operazioni.csv`")
    L.append("")

    L.append("## Riepilogo")
    L.append("")
    L.append(tabella(
        ["Voce", "Importo"],
        [
            ["Eseguiti registrati", f"{len(ops)} ({len(acquisti)} acquisti, {len(vendite)} vendite)"],
            ["Totale esborsi per acquisti (commissioni incluse)", eur(tot_acq)],
            ["Totale incassi netti da vendite", eur(tot_ven)],
            ["Commissioni e oneri registrati", eur(tot_comm)],
            ["Plusvalenze realizzate", eur(plus)],
            ["Minusvalenze realizzate", eur(minus)],
            ["**Risultato netto realizzato**", f"**{eur(plus + minus)}**"],
        ],
        allinea_dx={1},
    ))
    L.append("")

    L.append("## Registro cronologico degli eseguiti")
    L.append("")
    L.append(tabella(
        ["Data", "Tipo", "Titolo", "ISIN", "Mercato", "Q.tà", "Prezzo", "Controv. lordo",
         "Commissioni", "Netto", "N. ordine"],
        [[
            data_it(o.data), o.tipo, o.titolo, o.isin, o.mercato, qta(o.quantita),
            it(o.prezzo, 4), eur(o.controvalore_lordo), eur(o.commissioni),
            eur(o.esborso_o_incasso_netto), o.n_ordine or "—",
        ] for o in ops],
        allinea_dx={5, 6, 7, 8, 9},
    ))
    L.append("")
    note_ops = [o for o in ops if o.note]
    if note_ops:
        L.append("Note sugli eseguiti:")
        L.append("")
        for o in note_ops:
            L.append(f"- {data_it(o.data)} {o.tipo} {o.titolo}: {o.note}")
        L.append("")

    if realizzi:
        L.append("## Plusvalenze e minusvalenze realizzate")
        L.append("")
        L.append(tabella(
            ["Data vendita", "Titolo", "ISIN", "Q.tà", "Incasso netto", "Costo fiscale",
             "Plus/minus", "Rend. %", "Giorni"],
            [[
                data_it(r.vendita.data), r.vendita.titolo, r.vendita.isin, qta(r.vendita.quantita),
                eur(r.incasso_netto), eur(r.costo_fiscale), eur(r.risultato),
                it(r.rendimento_pct) + " %", str(r.giorni_detenzione),
            ] for r in realizzi],
            allinea_dx={3, 4, 5, 6, 7, 8},
        ))
        L.append("")
        for r in realizzi:
            v = r.vendita
            L.append(f"### {v.titolo} ({v.isin}) — vendita del {data_it(v.data)}")
            L.append("")
            righe = []
            for d, q, c in r.lotti_scaricati:
                righe.append([f"Carico del {data_it(d)}", qta(q), it(c / q, 4) if q else "—", eur(c)])
            righe.append(["**Costo fiscale scaricato**", qta(v.quantita), "", f"**{eur(r.costo_fiscale)}**"])
            righe.append([f"Vendita lorda{(' (ord. ' + v.n_ordine + ')') if v.n_ordine else ''}",
                          qta(v.quantita), it(v.prezzo, 4), eur(v.controvalore_lordo)])
            righe.append(["Commissioni di vendita", "", "", f"- {eur(v.commissioni)}"])
            righe.append(["**Incasso netto**", "", "", f"**{eur(r.incasso_netto)}**"])
            segno = "Plusvalenza" if r.risultato >= 0 else "Minusvalenza"
            righe.append([f"**{segno} realizzata**", "", "", f"**{eur(r.risultato)}** ({it(r.rendimento_pct)} %)"])
            L.append(tabella(["Voce", "Q.tà", "Prezzo unitario", "Importo"], righe, allinea_dx={1, 2, 3}))
            L.append("")

    if aperte:
        L.append("## Posizioni ancora in portafoglio")
        L.append("")
        L.append(tabella(
            ["Titolo", "ISIN", "Q.tà", "Costo medio di carico", "Costo totale di carico"],
            [[p.titolo, p.isin, qta(p.quantita), eur(p.costo_totale / p.quantita), eur(p.costo_totale)]
             for p in aperte],
            allinea_dx={2, 3, 4},
        ))
        L.append("")

    if avvisi:
        L.append("## Avvisi")
        L.append("")
        L += [f"- {a}" for a in avvisi]
        L.append("")

    L.append("## Note per il commercialista")
    L.append("")
    L.append("- Il costo fiscale include le commissioni di acquisto; l'incasso è al netto delle commissioni di vendita.")
    L.append("- Dove le commissioni di acquisto non sono state indicate separatamente, sono considerate incluse nel controvalore registrato (vedi note sugli eseguiti).")
    L.append("- Per titoli esteri verificare eventuali imposte sulle transazioni finanziarie locali (es. TTF francese 0,3% sugli acquisti di azioni francesi con capitalizzazione > 1 mld €) applicate dall'intermediario.")
    L.append("- L'intestatario è una società di capitali: il risultato concorre al reddito d'impresa secondo la classificazione in bilancio dei titoli (attivo circolante / immobilizzazioni finanziarie), non alla tassazione sostitutiva del 26% prevista per le persone fisiche.")
    L.append("- Date di regolamento (valuta) non riportate: per Euronext il ciclo è T+2; verificare sulle contabili bancarie.")
    L.append("")
    L.append("## Documenti da allegare")
    L.append("")
    for o in ops:
        L.append(f"- [ ] Eseguito / nota informativa {o.tipo.lower()} {o.titolo} del {data_it(o.data)}"
                 + (f" (ordine {o.n_ordine})" if o.n_ordine else ""))
    L.append("- [ ] Estratto conto titoli al 31/12 (o alla data del report)")
    L.append("- [ ] Movimenti del conto corrente di regolamento collegati agli eseguiti")
    L.append("")
    return "\n".join(L)


def scrivi_csv_plusvalenze(percorso: Path, realizzi: list[Realizzo]) -> None:
    with percorso.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["data_vendita", "titolo", "isin", "quantita", "prezzo_vendita", "controvalore_lordo",
                    "commissioni_vendita", "incasso_netto", "costo_fiscale", "plus_minusvalenza",
                    "rendimento_pct", "giorni_detenzione", "n_ordine"])
        for r in realizzi:
            v = r.vendita
            w.writerow([
                v.data.isoformat(), v.titolo, v.isin, qta(v.quantita).replace(".", ""),
                f"{v.prezzo:.4f}", f"{v.controvalore_lordo:.2f}", f"{v.commissioni:.2f}",
                f"{r.incasso_netto:.2f}", f"{r.costo_fiscale:.2f}", f"{r.risultato:.2f}",
                f"{r.rendimento_pct:.2f}", r.giorni_detenzione, v.n_ordine,
            ])


def scrivi_csv_posizioni(percorso: Path, aperte: list[Posizione]) -> None:
    with percorso.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["titolo", "isin", "quantita", "costo_medio", "costo_totale"])
        for p in aperte:
            w.writerow([p.titolo, p.isin, f"{p.quantita.normalize():f}",
                        f"{p.costo_totale / p.quantita:.4f}", f"{p.costo_totale:.2f}"])


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anno", type=int, default=date.today().year, help="esercizio da rendicontare (default: anno corrente)")
    ap.add_argument("--metodo", choices=METODI, default="medio", help="metodo di scarico dei lotti (default: medio)")
    ap.add_argument("--intestatario", default="AURORAE CAPITAL S.R.L.", help="intestatario del dossier titoli")
    ap.add_argument("--registro", type=Path, default=REGISTRO, help="percorso del CSV delle operazioni")
    ap.add_argument("--out", type=Path, default=REPORT_DIR, help="cartella di output")
    args = ap.parse_args(argv)

    tutte = leggi_registro(args.registro)
    # Le vendite dell'esercizio scaricano anche carichi di anni precedenti,
    # quindi si elabora tutto lo storico e si filtra solo la presentazione.
    realizzi, aperte, avvisi = elabora(tutte, args.metodo)
    ops_anno = [o for o in tutte if o.data.year == args.anno]
    realizzi_anno = [r for r in realizzi if r.vendita.data.year == args.anno]
    if not ops_anno:
        print(f"Nessuna operazione registrata per l'esercizio {args.anno}.", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    md = args.out / f"report_{args.anno}.md"
    md.write_text(genera_markdown(args.anno, args.metodo, ops_anno, realizzi_anno, aperte, avvisi,
                                  args.intestatario), encoding="utf-8")
    scrivi_csv_plusvalenze(args.out / f"plusvalenze_{args.anno}.csv", realizzi_anno)
    pos_csv = args.out / "posizioni_aperte.csv"
    if aperte:
        scrivi_csv_posizioni(pos_csv, aperte)
    elif pos_csv.exists():
        pos_csv.unlink()

    netto = sum((r.risultato for r in realizzi_anno), Decimal(0))
    print(f"Report scritto in {md}")
    print(f"Esercizio {args.anno}: {len(ops_anno)} eseguiti, risultato netto realizzato {eur(netto)}")
    for a in avvisi:
        print(f"AVVISO: {a}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
