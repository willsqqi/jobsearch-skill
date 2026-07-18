"use strict";

document.querySelector("#submit").addEventListener("click", () => {
  const warning = document.querySelector("#submission-warning");
  warning.hidden = false;
  warning.textContent = "Synthetic fixture: submission is disabled and no request was sent.";
});
