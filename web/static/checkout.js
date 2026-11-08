/* Checkout: hold the quoted amount with PayPal.
 *
 * Live (PayPal sandbox), JavaScript SDK v6:
 *   1. load https://www.sandbox.paypal.com/web-sdk/v6/core (async, init in its onload)
 *   2. createInstance({ clientToken, components: ["paypal-payments"], pageType: "checkout" }),
 *      with a browser-safe client token from GET /api/client-token
 *   3. findEligibleMethods({ currencyCode: "USD", amount }) and show <paypal-button> only if
 *      isEligible("paypal")
 *   4. createPayPalOneTimePaymentSession({ onApprove, onCancel, onError })
 *   5. on click: session.start({ presentationMode: "auto" }, createOrder()), where createOrder()
 *      is a Promise of { orderId } from POST /api/orders (intent AUTHORIZE, priced by the server)
 *   6. onApprove({ orderId }): POST /api/orders/{orderId}/authorize, then the live status page
 *   7. if PayPal declines the funding source (code instrument_declined), the order stays approved:
 *      start the session again on the same order so the buyer picks another funding source
 *
 * Offline: the same two server endpoints, with a simulated buyer approval in between.
 */
(function () {
  "use strict";
  var body = document.body;
  if (body.getAttribute("data-page") !== "run") return;
  var K = window.Keyhold;
  var rescueId = body.getAttribute("data-rescue-id");
  var amount = body.getAttribute("data-amount");
  var statusLine = document.getElementById("checkout-status");
  if (!rescueId || !statusLine) return;

  function say(text, tone) {
    statusLine.textContent = text;
    if (tone) statusLine.setAttribute("data-tone", tone);
    else statusLine.removeAttribute("data-tone");
  }

  /* The browser names the rescue only; the server prices the order from the stored quote. */
  function createOrder() {
    return K.api("POST", "/api/orders", { rescueId: rescueId }).then(function (data) {
      say("Order for a $" + data.amount + " hold created. Approve it in PayPal.");
      return { orderId: data.orderId };
    });
  }

  async function authorize(orderId) {
    say("Approved. Asking PayPal for the hold…");
    var data = await K.api("POST", "/api/orders/" + encodeURIComponent(orderId) + "/authorize");
    if (data.state === "VOIDED") {
      say("PayPal did not give a clean hold of the quote, so it was released. Nothing is charged.", "warn");
    } else {
      say("PayPal holds $" + (data.amount || amount) + ". Lending the keyboard and opening the live page…", "ok");
    }
    K.reloadTo(data.statusUrl);
  }

  /* Offline: the button says aria-disabled while the approval runs (a disabled button would drop
   * keyboard focus); on an error the message is announced and focus goes back to the button. */
  if (body.getAttribute("data-mode") === "offline") {
    var offlineButton = document.getElementById("offline-approve");
    if (!offlineButton) return;
    offlineButton.addEventListener("click", async function () {
      if (offlineButton.getAttribute("aria-disabled") === "true") return;
      offlineButton.setAttribute("aria-disabled", "true");
      try {
        var order = await createOrder();
        say("The simulated buyer approves the order in FakePayPal…");
        await K.api("POST", "/api/offline/orders/" + encodeURIComponent(order.orderId) + "/approve");
        await authorize(order.orderId);
      } catch (error) {
        say(error.message, "warn");
        offlineButton.removeAttribute("aria-disabled");
        offlineButton.focus();
      }
    });
    return;
  }

  var paypalButton = document.getElementById("paypal-button");
  var loading = document.getElementById("paypal-loading");
  var hardFailure = false;

  /* A definite failure (the SDK did not load, PayPal is not offered) is never overwritten by the
   * softer "taking long" note that a timer writes later. */
  function failed(text, hard) {
    if (hardFailure && !hard) return;
    if (hard) hardFailure = true;
    if (loading) {
      loading.textContent = text; // a status region: the failure is announced
      loading.hidden = false;
    }
  }
  // PayPal's script can be slow or blocked; say so instead of leaving a dashed box.
  var sdkTimer = window.setTimeout(function () {
    if (paypalButton && paypalButton.hidden) failed("PayPal is taking long to load. Check that pop-ups and PayPal are not blocked, then reload. Nothing is held.");
  }, 15000);

  async function onPayPalWebSdkLoaded() {
    try {
      var token = await K.api("GET", "/api/client-token");
      var sdkInstance = await window.paypal.createInstance({
        clientToken: token.clientToken,
        components: ["paypal-payments"],
        pageType: "checkout"
      });
      var methods = await sdkInstance.findEligibleMethods({ currencyCode: "USD", amount: amount });
      if (!methods.isEligible("paypal")) {
        failed("PayPal is not offered for this payment in this browser. Nothing is held.", true);
        return;
      }
      var session = sdkInstance.createPayPalOneTimePaymentSession({
        onApprove: async function (data) {
          try {
            await authorize(data.orderId);
          } catch (error) {
            say(error.message, "warn");
            if (error && error.code === "instrument_declined") {
              /* Same order, same quote: PayPal shows the buyer's other funding sources. */
              try {
                await session.start({ presentationMode: "auto" }, Promise.resolve({ orderId: data.orderId }));
              } catch (restart) {
                say((restart && restart.message) || "PayPal could not be opened again.", "warn");
              }
            }
          }
        },
        onCancel: function () {
          say("You closed PayPal. Nothing is held.");
        },
        onError: function (error) {
          say("PayPal reported an error" + (error && error.code ? " (" + error.code + ")" : "") + ". Nothing is held.", "warn");
        }
      });
      window.clearTimeout(sdkTimer);
      if (loading) loading.hidden = true;
      paypalButton.hidden = false;
      paypalButton.addEventListener("click", async function () {
        say("");
        try {
          await session.start({ presentationMode: "auto" }, createOrder());
        } catch (error) {
          say((error && error.message) || "PayPal could not be opened.", "warn");
        }
      });
    } catch (error) {
      failed("PayPal could not start: " + ((error && error.message) || "unknown error") + ".", true);
    }
  }

  var script = document.createElement("script");
  script.src = body.getAttribute("data-sdk-url");
  script.async = true;
  script.onload = onPayPalWebSdkLoaded;
  script.onerror = function () {
    window.clearTimeout(sdkTimer);
    failed("The PayPal JS SDK could not be loaded. Check that PayPal is not blocked, then reload. Nothing is held.", true);
  };
  document.head.appendChild(script);
})();
