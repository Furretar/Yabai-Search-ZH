/* Login/signup form: show requirement text only when the field is invalid. */
(function () {
    "use strict";

    var rules = {
        username: function (value) {
            if (!value) return false;
            if (value.length > 150) return false;
            return /^[\p{L}\p{N}_.@+-]+$/u.test(value);
        },
        password1: function (value) {
            if (!value) return false;
            return !/^\d+$/.test(value);
        },
        password2: function (value, input) {
            var first = document.querySelector('input[name="password1"]');
            if (!value || !first) return false;
            return value === first.value;
        }
    };

    function update(field, force) {
        var input = field.querySelector("input");
        if (!input) return;
        var help = field.querySelector(".helptext");
        var hasServerError = Boolean(field.querySelector(".errorlist"));
        var rule = rules[input.name];
        var valid = rule ? rule(input.value, input) : true;
        var show = hasServerError || (Boolean(force || input.dataset.touched) && !valid);
        field.classList.toggle("field-invalid", show);
        if (help) help.classList.toggle("show", show);
    }

    var fields = document.querySelectorAll(".field");
    Array.prototype.forEach.call(fields, function (field) {
        var input = field.querySelector("input");
        if (!input) return;
        input.addEventListener("input", function () {
            input.dataset.touched = "true";
            update(field);
            if (input.name === "password1") {
                var confirmField = input.closest("form").querySelector(".field input[name=password2]");
                if (confirmField) update(confirmField.closest(".field"));
            }
        });
        input.addEventListener("blur", function () {
            input.dataset.touched = "true";
            update(field, true);
        });
        update(field);
    });
})();
