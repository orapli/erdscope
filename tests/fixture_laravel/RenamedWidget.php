<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class RenamedWidget extends Model
{
    // naive class_to_table('RenamedWidget') would guess 'renamed_widgets' —
    // this override means the real table is 'crm_widgets' instead, and every
    // OTHER model's bare `RenamedWidget::class` reference must be redirected
    // to match (see Gadget::widget()).
    protected $table = 'crm_widgets';
}
