// Wardence traffic generator -- periodic burst (short, heavier spike).
// Fired by run.sh every ~10min alongside the continuous baseline.js.
// Deliberately lighter than baseline (browse + add-to-cart only, no
// register/address/card/checkout) -- bursts exist for extra volume
// variance, not checkout coverage (baseline already covers that
// continuously). Also kept modest (3x baseline VUs) so it stays well
// under anything that would push a service toward its own resource
// limits and start looking like a fault rather than organic variance.
//
// No login here, so POST /cart resolves the customer via the ANONYMOUS
// guest path (helpers.getCustomerId falls back to req.session.id when
// no "logged_in" cookie is present) -- confirmed against front-end's
// actual route/helper code, same source as baseline.js.

import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.TARGET_URL || "http://front-end.sock-shop.svc.cluster.local";

export const options = {
  scenarios: {
    burst: {
      executor: "constant-vus",
      vus: 6,
      duration: __ENV.DURATION || "30s",
    },
  },
};

export default function () {
  const catalogueRes = http.get(`${BASE_URL}/catalogue?size=10`);
  check(catalogueRes, { "catalogue 200": (r) => r.status === 200 });

  let itemId = null;
  try {
    const items = JSON.parse(catalogueRes.body);
    if (Array.isArray(items) && items.length > 0) {
      itemId = items[Math.floor(Math.random() * items.length)].id;
    }
  } catch (e) {
    // catalogue response shape unexpected -- skip the rest of this flow
  }

  if (!itemId) {
    sleep(0.2);
    return;
  }

  const detailRes = http.get(`${BASE_URL}/catalogue/${itemId}`);
  check(detailRes, { "detail 200": (r) => r.status === 200 });

  const cartRes = http.post(
    `${BASE_URL}/cart`,
    JSON.stringify({ id: itemId }),
    { headers: { "Content-Type": "application/json" } }
  );
  check(cartRes, { "add to cart ok": (r) => r.status === 200 || r.status === 201 });

  sleep(0.2);
}
