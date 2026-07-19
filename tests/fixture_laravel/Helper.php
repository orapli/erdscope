<?php

namespace App\Support;

// a plain, non-Eloquent class — must never become a phantom table just
// because it lives alongside the real models.
class Helper
{
    public static function noop()
    {
        return null;
    }
}
