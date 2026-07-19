<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Gadget extends Model
{
    public function widget()
    {
        return $this->belongsTo(RenamedWidget::class);
    }
}
