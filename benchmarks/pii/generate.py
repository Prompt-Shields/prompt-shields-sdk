"""Generate the labelled Nordic/EU PII benchmark (#35).

Every example is SYNTHETIC. Nothing here comes from real people or records:

- names are combinations of common first and last names, never paired with
  any real attribute;
- emails use reserved domains (RFC 2606: example.com/.org/.net, *.example);
- phone numbers use the fiction ranges where a regulator publishes one
  (SE 070-1740605..99, UK 07700 900xxx, FR 06 39 98 xx xx) and random digits
  elsewhere;
- national IDs, IBANs and card numbers are random but checksum-valid, so a
  detector cannot tell them from real ones (that is the point), yet they are
  not taken from anyone;
- IP addresses come from the documentation ranges (RFC 5737, RFC 3849).

The output is deterministic (fixed seed), so the committed dataset can be
checked against this script:

    python benchmarks/pii/generate.py            # rewrite dataset.v1.jsonl
    python benchmarks/pii/generate.py --check    # fail if it would change

Record fields: id, locale, lang, kind (positive | negative | hard_negative),
text, labels (sorted categories present in the text).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.v1.jsonl"
SEED = 35

LOCALES = {
    # locale: (lang, first names, last names)
    "se": ("sv", ["Anna", "Erik", "Maria", "Lars", "Karin", "Johan", "Elin", "Oskar"],
           ["Andersson", "Johansson", "Lindqvist", "Berg", "Nyström", "Holm"]),
    "no": ("nb", ["Ingrid", "Ola", "Kari", "Sindre", "Marit", "Henrik"],
           ["Hansen", "Johansen", "Olsen", "Haugen", "Berntsen", "Solberg"]),
    "dk": ("da", ["Mette", "Søren", "Freja", "Mads", "Ida", "Rasmus"],
           ["Jensen", "Nielsen", "Pedersen", "Kristensen", "Mikkelsen"]),
    "fi": ("fi", ["Aino", "Mikko", "Eeva", "Juha", "Sanna", "Ville"],
           ["Virtanen", "Korhonen", "Mäkinen", "Nieminen", "Laine"]),
    "de": ("de", ["Lukas", "Anna", "Jonas", "Lea", "Felix", "Sophie"],
           ["Müller", "Schmidt", "Schneider", "Fischer", "Weber"]),
    "fr": ("fr", ["Camille", "Lucas", "Chloé", "Hugo", "Manon", "Louis"],
           ["Martin", "Bernard", "Dubois", "Lefèvre", "Moreau"]),
    "nl": ("nl", ["Daan", "Emma", "Sem", "Julia", "Bram", "Sanne"],
           ["de Jong", "Jansen", "de Vries", "van Dijk", "Bakker"]),
    "es": ("es", ["Lucía", "Pablo", "Carmen", "Javier"],
           ["García", "Fernández", "López", "Martínez"]),
    "it": ("it", ["Giulia", "Marco", "Francesca", "Luca"],
           ["Rossi", "Bianchi", "Romano", "Colombo"]),
    "pl": ("pl", ["Zofia", "Jakub", "Anna", "Piotr"],
           ["Nowak", "Kowalski", "Wiśniewska", "Wójcik"]),
    "gb": ("en", ["Oliver", "Amelia", "Jack", "Isla"],
           ["Smith", "Jones", "Taylor", "Brown"]),
}

# Sentence templates per language. Placeholders: {name} {email} {phone}
# {iban} {nid} {card} {ip} {addr}. Labels follow from the placeholders used
# (plus the extra labels given with health/financial templates).
T = {
    "sv": {
        "email": ["Hej! Du når mig på {email}.", "Skicka kvittot till {email} tack.",
                  "Ny kontakt: {name}, e-post {email}"],
        "phone": ["Ring mig på {phone} efter lunch.", "Mitt mobilnummer är {phone}.",
                  "{name} nås på tel. {phone}"],
        "iban": ["Betala till mitt IBAN {iban}.", "Kontonummer (IBAN): {iban}",
                 "Pengarna ska sättas in på {iban}."],
        "card": ["Kortnummer {card}, giltigt till 08/29.", "Debitera kortet {card}."],
        "ip": ["Inloggningen kom från {ip}.", "Servern svarar inte på {ip}."],
        "nid": ["Mitt personnummer är {nid}.", "Personnr: {nid}",
                "Patienten {name} ({nid}) är inskriven."],
        "health": ["{name} har fått diagnosen typ 2-diabetes och behandlas med metformin.",
                   "Blodtrycket var 150/95 vid besöket, läkaren skrev ut ny medicin."],
        "financial": ["Uppdatera kontonumret i lönesystemet innan den 25:e.",
                      "Vilket clearingnummer har banken?"],
        "contact": ["{name}, {addr}", "Leverans till {name}, {addr}."],
    },
    "nb": {
        "email": ["Send svaret til {email}.", "E-posten min er {email}.",
                  "Kontakt {name} på {email}"],
        "phone": ["Du kan ringe meg på {phone}.", "Mobil: {phone}",
                  "{name} har telefon {phone}"],
        "iban": ["Overfør beløpet til IBAN {iban}.", "Kontoen min: {iban}",
                 "Utbetaling til {iban}"],
        "card": ["Kortnummeret er {card}.", "Belast {card} for abonnementet."],
        "ip": ["Forespørselen kom fra {ip}.", "Blokker adressen {ip}."],
        "nid": ["Fødselsnummeret mitt er {nid}.", "Fnr. {nid}",
                "{name}, fødselsnummer {nid}"],
        "health": ["{name} ble diagnostisert med astma og fikk resept på inhalator.",
                   "Legen noterte høyt blodtrykk og endret medisinen."],
        "financial": ["Husk å oppgi kontonummeret ved utbetaling.",
                      "Hva er kontonummeret til regnskapsavdelingen?"],
        "contact": ["{name}, {addr}", "Pakken sendes til {name}, {addr}."],
    },
    "da": {
        "email": ["Skriv til mig på {email}.", "Min mail er {email}.",
                  "Kontaktperson: {name} ({email})"],
        "phone": ["Ring på {phone} i dag.", "Telefon: {phone}",
                  "{name} kan træffes på {phone}"],
        "iban": ["Indbetal til IBAN {iban}.", "Kontonummer: {iban}",
                 "Refusion til {iban}"],
        "card": ["Kortnummer {card}.", "Træk beløbet på kortet {card}."],
        "ip": ["Login fra {ip} blev afvist.", "Klienten bruger {ip}."],
        "nid": ["Mit CPR-nummer er {nid}.", "CPR: {nid}",
                "{name}, cpr.nr. {nid}"],
        "health": ["{name} fik stillet diagnosen migræne og fik ordineret medicin.",
                   "Blodtrykket var forhøjet ved sidste kontrol hos lægen."],
        "financial": ["Opdater kontonummeret til lønudbetaling.",
                      "Send mig dit reg.nr. og kontonummer."],
        "contact": ["{name}, {addr}", "Levering til {name}, {addr}."],
    },
    "fi": {
        "email": ["Lähetä vastaus osoitteeseen {email}.", "Sähköpostini on {email}.",
                  "Yhteyshenkilö {name}, {email}"],
        "phone": ["Soita minulle numeroon {phone}.", "Puhelin: {phone}",
                  "{name} vastaa numerossa {phone}"],
        "iban": ["Maksa tilille IBAN {iban}.", "Tilinumero: {iban}",
                 "Palautus tilille {iban}"],
        "card": ["Kortin numero {card}.", "Veloita kortilta {card}."],
        "ip": ["Kirjautuminen osoitteesta {ip}.", "Estä osoite {ip}."],
        "nid": ["Henkilötunnukseni on {nid}.", "Hetu: {nid}",
                "{name}, henkilötunnus {nid}"],
        "health": ["{name} sai diagnoosin kakkostyypin diabetes ja lääkityksen.",
                   "Verenpaine oli koholla, lääkäri määräsi uuden lääkkeen."],
        "financial": ["Päivitä tilinumero palkanmaksua varten.",
                      "Mikä on yrityksen tilinumero?"],
        "contact": ["{name}, {addr}", "Toimitus: {name}, {addr}."],
    },
    "de": {
        "email": ["Bitte schreiben Sie an {email}.", "Meine E-Mail-Adresse: {email}",
                  "Ansprechpartner: {name}, {email}"],
        "phone": ["Rufen Sie mich unter {phone} an.", "Tel.: {phone}",
                  "{name} ist unter {phone} erreichbar"],
        "iban": ["Bitte überweisen Sie auf IBAN {iban}.", "Bankverbindung: {iban}",
                 "Erstattung auf das Konto {iban}"],
        "card": ["Kreditkartennummer {card}.", "Bitte die Karte {card} belasten."],
        "ip": ["Zugriff von {ip} wurde blockiert.", "Der Server unter {ip} ist offline."],
        "nid": [],
        "health": ["Bei {name} wurde Diabetes Typ 2 festgestellt, Metformin verordnet.",
                   "Der Blutdruck war erhöht, die Ärztin passte das Rezept an."],
        "financial": ["Bitte die Kontonummer in der Gehaltsabrechnung ändern.",
                      "Wie lautet die Steuernummer der Firma?"],
        "contact": ["{name}, {addr}", "Lieferung an {name}, {addr}."],
    },
    "fr": {
        "email": ["Écrivez-moi à {email}.", "Mon adresse e-mail : {email}",
                  "Contact : {name}, {email}"],
        "phone": ["Appelez-moi au {phone}.", "Tél. : {phone}",
                  "{name} est joignable au {phone}"],
        "iban": ["Virement sur l'IBAN {iban}.", "RIB : {iban}",
                 "Remboursement sur le compte {iban}"],
        "card": ["Numéro de carte {card}.", "Débitez la carte {card}."],
        "ip": ["Connexion depuis {ip}.", "Le serveur {ip} ne répond plus."],
        "nid": [],
        "health": ["{name} a reçu un diagnostic d'asthme et une ordonnance.",
                   "La tension artérielle était élevée, le médecin a changé le traitement."],
        "financial": ["Merci de mettre à jour le numéro de compte pour la paie.",
                      "Quel est le numéro fiscal de l'entreprise ?"],
        "contact": ["{name}, {addr}", "Livraison à {name}, {addr}."],
    },
    "nl": {
        "email": ["Mail me op {email}.", "Mijn e-mailadres is {email}.",
                  "Contactpersoon: {name}, {email}"],
        "phone": ["Bel me op {phone}.", "Telefoon: {phone}",
                  "{name} is bereikbaar op {phone}"],
        "iban": ["Maak het bedrag over naar IBAN {iban}.", "Rekeningnummer: {iban}",
                 "Terugbetaling naar {iban}"],
        "card": ["Creditcardnummer {card}.", "Schrijf af van kaart {card}."],
        "ip": ["Inlog vanaf {ip}.", "Blokkeer {ip} in de firewall."],
        "nid": [],
        "health": ["Bij {name} is diabetes vastgesteld; de huisarts schreef medicatie voor.",
                   "De bloeddruk was te hoog bij de laatste controle."],
        "financial": ["Pas het rekeningnummer aan in de salarisadministratie.",
                      "Wat is het btw-nummer van de leverancier?"],
        "contact": ["{name}, {addr}", "Bezorgen bij {name}, {addr}."],
    },
    "es": {
        "email": ["Escríbeme a {email}.", "Mi correo es {email}.",
                  "Contacto: {name}, {email}"],
        "phone": ["Llámame al {phone}.", "Teléfono: {phone}",
                  "{name} está en el {phone}"],
        "iban": ["Transfiere a la cuenta IBAN {iban}.", "Cuenta: {iban}",
                 "Reembolso a {iban}"],
        "card": ["Número de tarjeta {card}.", "Cargar en la tarjeta {card}."],
        "ip": ["Acceso desde {ip}.", "Bloquear la IP {ip}."],
        "nid": [],
        "health": ["{name} fue diagnosticada con asma y recibió una receta.",
                   "La presión arterial estaba alta en la última consulta."],
        "financial": ["Actualiza el número de cuenta para la nómina.",
                      "¿Cuál es el NIF de la empresa?"],
        "contact": ["{name}, {addr}", "Envío a {name}, {addr}."],
    },
    "it": {
        "email": ["Scrivimi a {email}.", "La mia email è {email}.",
                  "Referente: {name}, {email}"],
        "phone": ["Chiamami al {phone}.", "Tel.: {phone}",
                  "{name} risponde al {phone}"],
        "iban": ["Bonifico sull'IBAN {iban}.", "Conto corrente: {iban}",
                 "Rimborso su {iban}"],
        "card": ["Numero della carta {card}.", "Addebita sulla carta {card}."],
        "ip": ["Accesso da {ip}.", "Blocca l'indirizzo {ip}."],
        "nid": [],
        "health": ["A {name} è stata diagnosticata l'asma, con prescrizione di farmaci.",
                   "La pressione era alta durante l'ultima visita."],
        "financial": ["Aggiorna il numero di conto per lo stipendio.",
                      "Qual è la partita IVA del fornitore?"],
        "contact": ["{name}, {addr}", "Consegna a {name}, {addr}."],
    },
    "pl": {
        "email": ["Napisz do mnie na {email}.", "Mój e-mail to {email}.",
                  "Kontakt: {name}, {email}"],
        "phone": ["Zadzwoń pod {phone}.", "Tel.: {phone}",
                  "{name}, telefon {phone}"],
        "iban": ["Przelew na IBAN {iban}.", "Numer rachunku: {iban}",
                 "Zwrot na konto {iban}"],
        "card": ["Numer karty {card}.", "Obciąż kartę {card}."],
        "ip": ["Logowanie z {ip}.", "Zablokuj adres {ip}."],
        "nid": [],
        "health": ["U {name} zdiagnozowano astmę i wypisano receptę.",
                   "Ciśnienie krwi było podwyższone podczas wizyty."],
        "financial": ["Zaktualizuj numer rachunku do wypłaty.",
                      "Jaki jest NIP firmy?"],
        "contact": ["{name}, {addr}", "Dostawa do {name}, {addr}."],
    },
    "en": {
        "email": ["Please reply to {email}.", "My email is {email}.",
                  "Contact {name} at {email}"],
        "phone": ["Call me on {phone}.", "Mobile: {phone}",
                  "{name} can be reached on {phone}"],
        "iban": ["Please pay into IBAN {iban}.", "Account: {iban}",
                 "Refund to {iban}"],
        "card": ["Card number {card}, expiry 08/29.", "Charge the card {card}."],
        "ip": ["Login attempt from {ip}.", "Block {ip} at the firewall."],
        "nid": [],
        "health": ["Patient ID 4471: diagnosis of type 2 diabetes, medication adjusted.",
                   "{name}'s lab result came back and the prescription was renewed."],
        "financial": ["Please update the account number used for payroll.",
                      "What is the routing number for the supplier?"],
        "contact": ["{name}, {addr}", "Deliver to {name}, {addr}."],
    },
}

ADDRESSES = {
    "se": ["Storgatan {n}, 114 55 Stockholm", "Kungsgatan {n}, 411 19 Göteborg"],
    "no": ["Storgata {n}, 0155 Oslo", "Kongens gate {n}, 7011 Trondheim"],
    "dk": ["Vesterbrogade {n}, 1620 København V", "Søndergade {n}, 8000 Aarhus C"],
    "fi": ["Mannerheimintie {n} A 5, 00100 Helsinki", "Hämeenkatu {n}, 33100 Tampere"],
    "de": ["Hauptstraße {n}, 10115 Berlin", "Bahnhofstraße {n}, 80335 München"],
    "fr": ["{n} rue de la République, 69002 Lyon", "{n} avenue Victor Hugo, 75016 Paris"],
    "nl": ["Kerkstraat {n}, 1017 GB Amsterdam", "Dorpsstraat {n}, 3511 LK Utrecht"],
    "es": ["Calle Mayor {n}, 28013 Madrid", "Carrer Gran {n}, 08001 Barcelona"],
    "it": ["Via Roma {n}, 00184 Roma", "Corso Italia {n}, 20122 Milano"],
    "pl": ["ul. Długa {n}, 00-238 Warszawa", "ul. Polna {n}, 31-000 Kraków"],
    "gb": ["{n} High Street, Bristol BS1 2AW", "{n} Station Road, Leeds LS1 4DY"],
}

# Localised labels for look-alike (hard negative) numbers.
NEG_WORDS = {
    "sv": ("Ordernummer", "Faktura nr", "Kundnummer", "Datum", "Spårningsnummer", "Belopp", "Org.nr"),
    "nb": ("Ordrenummer", "Fakturanr.", "Kundenummer", "Dato", "Sporingsnummer", "Beløp", "Org.nr."),
    "da": ("Ordrenummer", "Fakturanr.", "Kundenummer", "Dato", "Trackingnummer", "Beløb", "CVR"),
    "fi": ("Tilausnumero", "Laskun numero", "Asiakasnumero", "Päivämäärä", "Seurantakoodi", "Summa", "Y-tunnus"),
    "de": ("Bestellnummer", "Rechnungsnr.", "Kundennummer", "Datum", "Sendungsnummer", "Betrag", "USt-IdNr."),
    "fr": ("Commande n°", "Facture n°", "Client n°", "Date", "Suivi colis", "Montant", "SIREN"),
    "nl": ("Bestelnummer", "Factuurnr.", "Klantnummer", "Datum", "Trackingcode", "Bedrag", "KvK"),
    "es": ("Pedido n.º", "Factura n.º", "Cliente n.º", "Fecha", "Seguimiento", "Importe", "CIF"),
    "it": ("Ordine n.", "Fattura n.", "Codice cliente", "Data", "Tracking", "Importo", "P.IVA"),
    "pl": ("Zamówienie nr", "Faktura nr", "Nr klienta", "Data", "Nr przesyłki", "Kwota", "REGON"),
    "en": ("Order no.", "Invoice no.", "Customer no.", "Date", "Tracking no.", "Amount", "Company no."),
}

NEUTRAL = {
    "sv": ["Kan du sammanfatta mötet i tre punkter?", "Översätt texten till engelska.",
           "Skriv ett vänligt svar till kunden om leveransen."],
    "nb": ["Kan du oppsummere møtet?", "Oversett dette til engelsk.",
           "Skriv et kort svar om leveringstiden."],
    "da": ["Kan du opsummere mødet?", "Oversæt teksten til engelsk.",
           "Skriv et venligt svar om leveringen."],
    "fi": ["Voitko tiivistää kokouksen?", "Käännä teksti englanniksi.",
           "Kirjoita ystävällinen vastaus asiakkaalle."],
    "de": ["Fasse das Meeting in drei Punkten zusammen.", "Übersetze den Text ins Englische.",
           "Schreibe eine freundliche Antwort zur Lieferung."],
    "fr": ["Résume la réunion en trois points.", "Traduis ce texte en anglais.",
           "Rédige une réponse polie au client."],
    "nl": ["Vat de vergadering samen.", "Vertaal de tekst naar het Engels.",
           "Schrijf een vriendelijk antwoord over de levering."],
    "es": ["Resume la reunión en tres puntos.", "Traduce el texto al inglés.",
           "Escribe una respuesta amable al cliente."],
    "it": ["Riassumi la riunione in tre punti.", "Traduci il testo in inglese.",
           "Scrivi una risposta cortese al cliente."],
    "pl": ["Podsumuj spotkanie w trzech punktach.", "Przetłumacz tekst na angielski.",
           "Napisz uprzejmą odpowiedź do klienta."],
    "en": ["Summarise the meeting in three bullet points.", "Translate this into Swedish.",
           "Write a friendly reply about the delivery date."],
}


# --- checksums ---------------------------------------------------------------

def luhn_digit(payload: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def mod11(digits: str, weights) -> int | None:
    rest = sum(int(d) * w for d, w in zip(digits, weights)) % 11
    k = 0 if rest == 0 else 11 - rest
    return None if k == 10 else k


def iban_check(country: str, bban: str) -> str:
    s = bban + country + "00"
    n = int("".join(str(int(c, 36)) for c in s))
    return f"{98 - n % 97:02d}"


# --- value generators ------------------------------------------------------------

class Gen:
    def __init__(self, rng: random.Random):
        self.r = rng

    def digits(self, n: int) -> str:
        return "".join(self.r.choice("0123456789") for _ in range(n))

    def birthdate(self, lo=1940, hi=2006) -> date:
        start = date(lo, 1, 1)
        return start + timedelta(days=self.r.randrange((date(hi, 12, 31) - start).days))

    # Nordic national IDs (checksum-valid) ---------------------------------
    def se_pnr(self) -> str:
        d = self.birthdate()
        day = d.day + (60 if self.r.random() < 0.15 else 0)  # samordningsnummer
        yy, mm, dd = f"{d.year % 100:02d}", f"{d.month:02d}", f"{day:02d}"
        serial = self.digits(3)
        check = luhn_digit(yy + mm + dd + serial)
        style = self.r.randrange(3)
        if style == 0:
            return f"{yy}{mm}{dd}-{serial}{check}"
        if style == 1:
            return f"{d.year // 100}{yy}{mm}{dd}-{serial}{check}"
        return f"{d.year // 100}{yy}{mm}{dd}{serial}{check}"

    def no_fnr(self) -> str:
        while True:
            d = self.birthdate()
            day, month = d.day, d.month
            kind = self.r.random()
            if kind < 0.15:
                day += 40  # D-number
            elif kind < 0.3:
                month += 80  # synthetic (Tenor) test number
            if d.year < 2000:
                ind = self.r.randrange(0, 500)
            else:
                ind = self.r.randrange(500, 1000)
            base = f"{day:02d}{month:02d}{d.year % 100:02d}{ind:03d}"
            k1 = mod11(base, (3, 7, 6, 1, 8, 9, 4, 5, 2))
            if k1 is None:
                continue
            k2 = mod11(base + str(k1), (5, 4, 3, 2, 7, 6, 5, 4, 3, 2))
            if k2 is None:
                continue
            fnr = f"{base}{k1}{k2}"
            return fnr if self.r.random() < 0.7 else f"{fnr[:6]} {fnr[6:]}"

    def dk_cpr(self) -> str:
        d = self.birthdate()
        yy = d.year % 100
        if d.year < 2000:
            d7 = self.r.randrange(0, 4)
        else:
            d7 = self.r.choice([4, 9]) if yy <= 36 else self.r.randrange(5, 9)
        head = f"{d.day:02d}{d.month:02d}{yy:02d}"
        compact = self.r.random() < 0.3
        while True:
            serial = f"{d7}{self.digits(3)}"
            if not compact:
                return f"{head}-{serial}"
            w = (4, 3, 2, 7, 6, 5, 4, 3, 2, 1)
            if sum(int(c) * k for c, k in zip(head + serial, w)) % 11 == 0:
                return head + serial

    def fi_hetu(self) -> str:
        d = self.birthdate()
        if d.year < 2000:
            marker = self.r.choice("-YXWVU")
        else:
            marker = self.r.choice("ABCDEF")
        ind = f"{self.r.randrange(2, 900):03d}"
        head = f"{d.day:02d}{d.month:02d}{d.year % 100:02d}"
        check = "0123456789ABCDEFHJKLMNPRSTUVWXY"[int(head + ind) % 31]
        return f"{head}{marker}{ind}{check}"

    # Other EU national IDs (checksum-valid; not covered by the detectors) --
    def nl_bsn(self) -> str:
        while True:
            body = self.digits(8)
            s = sum(int(c) * w for c, w in zip(body, range(9, 1, -1)))
            last = s % 11
            if last < 10 and body[0] != "0":
                return body + str(last)

    def pl_pesel(self) -> str:
        d = self.birthdate()
        mm = d.month + (20 if d.year >= 2000 else 0)
        body = f"{d.year % 100:02d}{mm:02d}{d.day:02d}{self.digits(4)}"
        w = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
        return body + str((10 - sum(int(c) * k for c, k in zip(body, w)) % 10) % 10)

    def es_dni(self) -> str:
        n = self.r.randrange(10_000_000, 99_999_999)
        return f"{n}{'TRWAGMYFPDXBNJZSQVHLCKE'[n % 23]}"

    def fr_nir(self) -> str:
        d = self.birthdate()
        body = f"{self.r.choice('12')}{d.year % 100:02d}{d.month:02d}{self.r.randrange(1, 96):02d}{self.digits(6)}"
        key = 97 - int(body) % 97
        return f"{body[0]} {body[1:3]} {body[3:5]} {body[5:7]} {body[7:10]} {body[10:13]} {key:02d}"

    def nid(self, locale: str) -> str:
        return {"se": self.se_pnr, "no": self.no_fnr, "dk": self.dk_cpr, "fi": self.fi_hetu,
                "nl": self.nl_bsn, "pl": self.pl_pesel, "es": self.es_dni,
                "fr": self.fr_nir}[locale]()

    # Phones ---------------------------------------------------------------
    def phone(self, locale: str) -> str:
        r, d = self.r, self.digits
        nn = f"{r.randrange(5, 100):02d}"
        options = {
            "se": [f"+46 70 174 06 {nn}", f"070-174 06 {nn}", f"+467017406{nn}", f"070 174 06 {nn}"],
            "no": [f"+47 4{d(2)} {d(2)} {d(3)}", f"9{d(2)} {d(2)} {d(3)}", f"+479{d(7)}",
                   f"4{d(1)} {d(2)} {d(2)} {d(2)}"],
            "dk": [f"+45 2{d(1)} {d(2)} {d(2)} {d(2)}", f"3{d(1)} {d(2)} {d(2)} {d(2)}",
                   f"+452{d(7)}", f"+45 {d(4)} {d(4)}"],
            "fi": [f"+358 40 {d(3)} {d(4)}", f"040 {d(3)} {d(4)}", f"+35850{d(7)}",
                   f"050-{d(3)} {d(4)}"],
            "de": [f"+49 30 {d(7)}", f"030 {d(8)}", f"+49 151 {d(8)}", f"0151/{d(8)}"],
            "fr": [f"+33 6 39 98 {d(2)} {d(2)}", f"06 39 98 {d(2)} {d(2)}", f"+33639 98{d(4)}",
                   f"06.39.98.{d(2)}.{d(2)}"],
            "nl": [f"+31 6 {d(8)}", f"06-{d(8)}", f"+316{d(8)}", f"020 {d(3)} {d(4)}"],
            "es": [f"+34 6{d(2)} {d(3)} {d(3)}", f"6{d(2)} {d(2)} {d(2)} {d(2)}", f"+346{d(8)}",
                   f"91 {d(3)} {d(2)} {d(2)}"],
            "it": [f"+39 3{d(2)} {d(3)} {d(4)}", f"3{d(2)} {d(3)} {d(4)}", f"+393{d(9)}",
                   f"06 {d(4)} {d(4)}"],
            "pl": [f"+48 5{d(2)} {d(3)} {d(3)}", f"5{d(2)} {d(3)} {d(3)}", f"+485{d(8)}",
                   f"5{d(2)}-{d(3)}-{d(3)}"],
            "gb": [f"+44 7700 900{d(3)}", f"07700 900{d(3)}", f"+447700900{d(3)}",
                   f"(07700) 900 {d(3)}"],
        }[locale]
        return r.choice(options)

    # IBANs ------------------------------------------------------------------
    IBAN_BBAN: ClassVar[dict[str, tuple[str, list[tuple[str, int]]]]] = {
        "se": ("SE", [("n", 20)]), "no": ("NO", [("n", 11)]), "dk": ("DK", [("n", 14)]),
        "fi": ("FI", [("n", 14)]), "de": ("DE", [("n", 18)]), "fr": ("FR", [("n", 23)]),
        "nl": ("NL", [("a", 4), ("n", 10)]), "es": ("ES", [("n", 20)]),
        "it": ("IT", [("a", 1), ("n", 22)]), "pl": ("PL", [("n", 24)]),
        "gb": ("GB", [("a", 4), ("n", 14)]),
    }

    def iban(self, locale: str) -> str:
        country, spec = self.IBAN_BBAN[locale]
        bban = "".join(
            "".join(self.r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(n))
            if kind == "a" else self.digits(n)
            for kind, n in spec
        )
        iban = country + iban_check(country, bban) + bban
        if self.r.random() < 0.5:
            return iban
        return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))

    # Cards ------------------------------------------------------------------
    def card(self) -> str:
        kind = self.r.randrange(3)
        if kind == 2:  # Amex 15 digits, 4-6-5
            payload = self.r.choice(["34", "37"]) + self.digits(12)
            n = payload + luhn_digit(payload)
            return f"{n[:4]} {n[4:10]} {n[10:]}"
        payload = (self.r.choice(["4"]) + self.digits(14) if kind == 0
                   else f"5{self.r.randrange(1, 6)}" + self.digits(13))
        n = payload + luhn_digit(payload)
        sep = self.r.choice([" ", "-", ""])
        return sep.join(n[i:i + 4] for i in range(0, 16, 4))

    def ip(self) -> str:
        if self.r.random() < 0.15:
            return f"2001:db8:{self.r.randrange(0, 0xffff):x}::{self.r.randrange(1, 0xffff):x}"
        net = self.r.choice(["192.0.2", "198.51.100", "203.0.113"])
        return f"{net}.{self.r.randrange(1, 255)}"


def ascii_fold(s: str) -> str:
    s = s.replace("ø", "o").replace("Ø", "O").replace("æ", "ae").replace("ß", "ss")
    s = s.replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def email(g: Gen, first: str, last: str) -> str:
    f = ascii_fold(first).lower()
    last_part = ascii_fold(last).lower().replace(" ", "")
    domain = g.r.choice(["example.com", "example.org", "example.net", "firma.example",
                         "mail.example", "sub.example.co.uk"])
    style = g.r.randrange(4)
    if style == 0:
        return f"{f}.{last_part}@{domain}"
    if style == 1:
        return f"{f[0]}{last_part}{g.r.randrange(1, 99)}@{domain}"
    if style == 2:
        return f"{f}+news@{domain}"
    return f"{f.upper()}.{last_part.upper()}@{domain.upper()}"


# --- assembly ------------------------------------------------------------------

PLACEHOLDER_LABEL = {
    "{name}": "person_name", "{email}": "email", "{phone}": "phone", "{iban}": "iban",
    "{nid}": "national_id", "{card}": "credit_card", "{ip}": "ip_address",
    "{addr}": "postal_address",
}


def build() -> list[dict]:
    rng = random.Random(SEED)
    g = Gen(rng)
    out: list[dict] = []
    counters: dict[str, int] = {}

    def add(locale: str, lang: str, kind: str, cat: str, text: str, labels: set[str]):
        key = f"{locale}-{cat}"
        counters[key] = counters.get(key, 0) + 1
        out.append({
            "id": f"{key}-{counters[key]:03d}",
            "locale": locale,
            "lang": lang,
            "kind": kind,
            "text": text,
            "labels": sorted(labels),
        })

    def fill(locale: str, tpl: str, first: str, last: str) -> tuple[str, set[str]]:
        labels = {lbl for ph, lbl in PLACEHOLDER_LABEL.items() if ph in tpl}
        values = {
            "name": f"{first} {last}",
            "email": email(g, first, last),
            "phone": g.phone(locale),
            "iban": g.iban(locale),
            "nid": g.nid(locale) if "{nid}" in tpl else "",
            "card": g.card(),
            "ip": g.ip(),
            "addr": rng.choice(ADDRESSES[locale]).format(n=rng.randrange(1, 120)),
        }
        return tpl.format(**values), labels

    for locale, (lang, firsts, lasts) in LOCALES.items():
        tpls = T[lang]
        reps = {"email": 2, "phone": 3, "iban": 2, "card": 2, "ip": 2, "nid": 4,
                "health": 1, "financial": 1, "contact": 1}
        for cat, n in reps.items():
            templates = list(tpls[cat])
            if cat == "nid" and not templates and locale in ("nl", "pl", "es", "fr"):
                templates = [{"nl": "Mijn BSN is {nid}.", "pl": "Mój PESEL to {nid}.",
                              "es": "Mi DNI es {nid}.",
                              "fr": "Mon numéro de sécurité sociale : {nid}"}[locale]]
                n = 2
            for _ in range(n):
                for tpl in templates:
                    text, labels = fill(locale, tpl, rng.choice(firsts), rng.choice(lasts))
                    if cat == "health":
                        labels.add("health_data")
                    if cat in ("financial", "iban"):
                        labels.add("financial_data")
                    add(locale, lang, "positive", cat, text, labels)

        # Mixed: several categories in one prompt.
        for _ in range(3):
            first, last = rng.choice(firsts), rng.choice(lasts)
            parts = [tpls["email"][1], tpls["phone"][1]]
            labels_extra = set()
            if tpls["nid"]:
                parts.append(tpls["nid"][1])
            if rng.random() < 0.5:
                parts.append(tpls["iban"][1])
                labels_extra.add("financial_data")
            text, labels = fill(locale, " ".join(parts), first, last)
            add(locale, lang, "positive", "mixed", text, labels | labels_extra)

        # Plain negatives.
        for text in NEUTRAL[lang]:
            add(locale, lang, "negative", "neutral", text, set())

        # Hard negatives: numbers and strings that look like PII but are not.
        order, invoice, customer, date_w, tracking, amount, org = NEG_WORDS[lang]
        d = g.birthdate(2018, 2026)
        hard = [
            f"{date_w}: {d.isoformat()}",
            f"{date_w}: {d.day:02d}.{d.month:02d}.{d.year}",
            f"{order} {g.digits(rng.choice([8, 10, 12]))}",
            f"{order} ORD-{d.year}-{g.digits(6)}",
            f"{invoice} {d.strftime('%Y%m%d')}-{g.digits(4)}",
            f"{invoice} {d.strftime('%y%m%d')}-{g.digits(4)}",
            f"{customer} {g.digits(11)}",
            f"{customer} {g.digits(3)} {g.digits(3)} {g.digits(3)}",
            f"{tracking} {g.digits(20)}",
            f"{tracking} JJD00{g.digits(14)}",
            f"EAN {g.digits(13)}",
            f"{amount}: {rng.randrange(1, 999)} {rng.randrange(100, 999)},{g.digits(2)}",
            f"{org} {g.digits(6)}-{g.digits(4)}",
            f"{org} {locale.upper()}{g.digits(10)}",
            f"Firmware 4.{rng.randrange(1, 20)}.{rng.randrange(0, 9)}.{rng.randrange(1, 200)}",
            f"Build {d.strftime('%Y%m%d%H%M%S')}",
            f"ref 4b1e{g.digits(4)}-9c2a-4f1d-8e7b-{g.digits(12)}",
            f"@{ascii_fold(rng.choice(firsts)).lower()}-team, 14:30–16:00",
            f"ISBN 978-91-{g.digits(2)}-{g.digits(5)}-{g.digits(1)}",
        ]
        for text in hard:
            add(locale, lang, "hard_negative", "lookalike", text, set())

    return out


def render(records: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the dataset would change")
    args = ap.parse_args(argv)
    text = render(build())
    if args.check:
        if not DATASET.exists() or DATASET.read_text(encoding="utf-8") != text:
            print(f"{DATASET} is out of date: run benchmarks/pii/generate.py", file=sys.stderr)
            return 1
        return 0
    DATASET.write_text(text, encoding="utf-8")
    print(f"wrote {DATASET} ({text.count(chr(10))} examples)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
