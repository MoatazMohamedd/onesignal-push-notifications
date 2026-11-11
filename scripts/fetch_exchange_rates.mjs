const api = "https://api.frankfurter.dev/v2";

async function getJson(path) {
  const response = await fetch(`${api}${path}`);
  if (!response.ok) throw new Error(`Frankfurter ${path} failed: ${response.status}`);
  return response.json();
}

const [catalogue, rates] = await Promise.all([
  getJson("/currencies"),
  getJson("/rates?base=USD"),
]);
const arabicNames = new Intl.DisplayNames(["ar"], { type: "currency" });
const currencyByCode = new Map(
  catalogue
    .filter((currency) => currency.type === "currency")
    .map((currency) => [currency.iso_code, currency]),
);
const currencies = rates
  .map(({ date, quote, rate }) => {
    const currency = currencyByCode.get(quote);
    if (!currency || !Number.isFinite(rate) || rate <= 0) return null;
    return {
      code: quote,
      nameEnglish: currency.name,
      nameArabic: arabicNames.of(quote) ?? currency.name,
      symbol: currency.symbol ?? null,
      rate,
      asOf: date,
    };
  })
  .filter(Boolean)
  .sort((left, right) => left.code.localeCompare(right.code));

if (!currencies.some((currency) => currency.code === "USD" && currency.rate === 1)) {
  throw new Error("Frankfurter response is missing the USD identity rate");
}

process.stdout.write(JSON.stringify({ baseCurrency: "USD", currencies }));
