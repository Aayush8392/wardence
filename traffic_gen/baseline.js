// Wardence traffic generator -- baseline (continuous low-rate) load.
// Simulates: register -> add address/card -> browse catalogue -> view
// item -> add to cart -> checkout.
// Purpose: keep metrics/logs non-flat for the replay viewer (P4) and
// give the future DL detector (P5) a real "normal" sequence to learn.
//
// Every request shape here is confirmed against front-end's actual
// route handlers (api/user, api/cart, api/orders in
// microservices-demo/front-end), not guessed:
//   - POST /register sets a "logged_in" cookie + session.customerId
//   - POST /addresses and POST /cards auto-attach the session's
//     userID server-side -- we never send it ourselves
//   - POST /cart takes {id: itemId} and resolves the customer via the
//     session (helpers.getCustomerId) -- registering BEFORE adding to
//     cart matters: cart items added while logged in use
//     session.customerId, the same id /orders looks up, so login has
//     to happen first or the order step won't find the cart
//   - POST /orders takes an empty-ish body; front-end builds the real
//     order server-side from the session (customer/address/card/items)
// k6's default per-VU cookie jar carries the session cookie across
// these calls automatically within one iteration.

import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.TARGET_URL || "http://front-end.sock-shop.svc.cluster.local";
// Real request-synced GC trigger design (2026-08-22 session, locked in
// wardence_buildlog.md): while a memory-leak episode is live, operator_api
// pauses ONLY this script's checkout step, never the whole script -- the
// other 6 steps never reach shipping anyway, so skipping just this one
// removes the one real competing request type while keeping the
// dashboard's own visible k6_http_reqs_total rate looking normal
// throughout the episode (a full-script pause would flatline it, a real,
// observable tell correlating with the trigger click).
const OPERATOR_API_URL = __ENV.OPERATOR_API_URL || "";

export const options = {
  scenarios: {
    baseline: {
      executor: "constant-vus",
      vus: 2,
      duration: __ENV.DURATION || "570s", // one gap between bursts (~9.5min)
    },
  },
};

function randomSuffix() {
  return Math.random().toString(36).slice(2, 10);
}

export default function () {
  const suffix = randomSuffix();
  const headers = { "Content-Type": "application/json" };

  // 1. register (establishes the session used by every later step)
  const registerRes = http.post(
    `${BASE_URL}/register`,
    JSON.stringify({
      username: `wardence_${suffix}`,
      password: "wardencePass123",
      email: `wardence_${suffix}@example.com`,
      firstName: "Wardence",
      lastName: "Trafficgen",
    }),
    { headers }
  );
  const registered = check(registerRes, { "register 200": (r) => r.status === 200 });

  if (!registered) {
    sleep(1);
    return;
  }

  sleep(0.3);

  // 2. add address (userID auto-attached server-side from the session)
  const addressRes = http.post(
    `${BASE_URL}/addresses`,
    JSON.stringify({
      street: "1 Wardence Way",
      number: "1",
      country: "UK",
      city: "London",
      postcode: "W1 1AA",
    }),
    { headers }
  );
  check(addressRes, { "address 200": (r) => r.status === 200 });

  sleep(0.3);

  // 3. add card (userID auto-attached server-side from the session)
  const cardRes = http.post(
    `${BASE_URL}/cards`,
    JSON.stringify({
      longNum: "1234567890123456",
      expires: "12/2030",
      ccv: "123",
    }),
    { headers }
  );
  check(cardRes, { "card 200": (r) => r.status === 200 });

  sleep(0.3);

  // 4. browse catalogue
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
    sleep(1);
    return;
  }

  // 5. view item detail
  const detailRes = http.get(`${BASE_URL}/catalogue/${itemId}`, {
    tags: { name: "catalogue_detail" },
  });
  check(detailRes, { "detail 200": (r) => r.status === 200 });

  sleep(0.3);

  // 6. add to cart -- customer resolved from the session, not sent by us
  const cartRes = http.post(`${BASE_URL}/cart`, JSON.stringify({ id: itemId }), { headers });
  check(cartRes, { "add to cart ok": (r) => r.status === 200 || r.status === 201 });

  sleep(0.3);

  // 7. checkout -- front-end builds the order server-side from the
  // session (customer, address, card, cart items) -- we just POST.
  // Skipped ONLY while operator_api reports a live memory-leak episode
  // (see the OPERATOR_API_URL comment above) -- if OPERATOR_API_URL is
  // unset (e.g. local testing outside the cluster), or the check itself
  // fails, this fails OPEN (checkout still fires) rather than silently
  // dropping real traffic on an unrelated outage.
  let leakPaused = false;
  if (OPERATOR_API_URL) {
    try {
      const pauseRes = http.get(`${OPERATOR_API_URL}/internal/leak-pause`, {
        tags: { name: "leak_pause_check" },
      });
      if (pauseRes.status === 200) {
        const body = JSON.parse(pauseRes.body);
        leakPaused = body && body.paused === true;
      }
    } catch (e) {
      // fail open -- see comment above
    }
  }

  if (!leakPaused) {
    const orderRes = http.post(`${BASE_URL}/orders`, "{}", { headers });
    check(orderRes, { "checkout 200": (r) => r.status === 200 });
  }

  sleep(1);
}
