<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class DynamicTarget extends Model
{
    public function owner()
    {
        // the related model can't be resolved statically (a variable, not a
        // `Foo::class` reference or a quoted string) — must warn with a
        // file:line and skip this association, never silently drop it
        // without a trace or silently guess a wrong target.
        $class = $this->resolveOwnerClass();
        return $this->belongsTo($class);
    }
}
