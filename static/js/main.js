/* ============================================================
   DocPilot AI — общие скрипты (счётчик, печать)
   Курсор-шлейф отключён по фидбэку пользователя (раздражал).
   ============================================================ */


function countUp(el, target, duration) {

    const startTime = performance.now();

    function tick(now) {

        const progress = Math.min((now - startTime) / duration, 1);

        const value = Math.floor(progress * target);

        el.textContent = value.toLocaleString("ru-RU");

        if (progress < 1) {
            requestAnimationFrame(tick);
        } else {
            el.textContent = target.toLocaleString("ru-RU");
        }
    }

    requestAnimationFrame(tick);
}


document.addEventListener("DOMContentLoaded", function () {

    const counters = document.querySelectorAll("[data-count]");

    if (!counters.length) return;

    const observer = new IntersectionObserver(function (entries) {

        entries.forEach(function (entry) {

            if (entry.isIntersecting) {

                const el = entry.target;

                const target = parseInt(el.dataset.count, 10) || 0;

                countUp(el, target, 1400);

                observer.unobserve(el);
            }
        });

    }, { threshold: 0.4 });

    counters.forEach(function (c) {
        observer.observe(c);
    });

});


function typeEffect(el, speed) {

    const raw = el.dataset.typeText || "";

    const text = raw.replace(/\\n/g, "\n");

    el.innerHTML = "";

    el.classList.add("typing");

    let i = 0;

    function step() {

        if (i <= text.length) {

            el.innerHTML = text.slice(0, i).replace(/\n/g, "<br>");

            i++;

            setTimeout(step, speed);

        } else {

            el.classList.remove("typing");
            el.classList.add("typing-done");
        }
    }

    step();
}


document.addEventListener("DOMContentLoaded", function () {

    document.querySelectorAll("[data-type-text]").forEach(function (el) {

        const speed = parseInt(el.dataset.typingSpeed, 10) || 32;

        typeEffect(el, speed);
    });

});
