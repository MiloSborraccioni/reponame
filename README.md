# Tracker compravendita titoli

Registro delle operazioni di acquisto/vendita titoli e generatore del report da consegnare al commercialista.

## Struttura

| File | Cosa contiene |
| --- | --- |
| `registro_operazioni.csv` | Il registro: una riga per ogni eseguito (acquisto o vendita). È l'unica cosa da aggiornare a mano. |
| `genera_report.py` | Script (solo Python standard, nessuna installazione) che legge il registro e produce il report. |
| `report/report_<anno>.md` | Report leggibile: riepilogo, registro cronologico, plus/minusvalenze con riconciliazione per ogni vendita, note e checklist documenti. |
| `report/plusvalenze_<anno>.csv` | Una riga per ogni vendita con incasso netto, costo fiscale e plus/minusvalenza (importabile in Excel). |
| `report/posizioni_aperte.csv` | Titoli ancora in carico con costo medio (creato solo se ci sono posizioni aperte). |

## Come registrare un'operazione

Aggiungere una riga a `registro_operazioni.csv` (separatore `;`, decimali con il punto):

| Colonna | Note |
| --- | --- |
| `data` | Data di esecuzione in formato `AAAA-MM-GG` |
| `tipo` | `Acquisto` oppure `Vendita` |
| `titolo`, `isin`, `mercato` | Come riportati sull'eseguito |
| `quantita` | Quantità eseguita |
| `prezzo` | Prezzo di esecuzione (se vuoto viene ricavato da controvalore ÷ quantità) |
| `controvalore_lordo` | CTVL lordo dell'eseguito (se vuoto viene calcolato come quantità × prezzo) |
| `commissioni` | Commissioni e oneri dell'operazione (il "Costo operazione" dell'eseguito) |
| `n_ordine`, `conto_titoli` | Riferimenti per ritrovare l'eseguito |
| `note` | Qualsiasi precisazione utile al commercialista |

## Come generare il report

```bash
python3 genera_report.py                 # esercizio corrente, costo medio ponderato
python3 genera_report.py --anno 2026     # esercizio specifico
python3 genera_report.py --metodo fifo   # metodo di scarico: medio | fifo | lifo
```

Il costo fiscale di ogni vendita include le commissioni di acquisto; l'incasso è al netto delle commissioni di vendita. Le vendite scaricano anche i carichi degli anni precedenti presenti nel registro, quindi conviene tenere tutto lo storico in un unico file.

## Cosa consegnare al commercialista

1. `report/report_<anno>.md` (o la sua esportazione in PDF).
2. `report/plusvalenze_<anno>.csv`.
3. Gli eseguiti/note informative dell'intermediario elencati nella checklist in fondo al report.
