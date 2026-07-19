<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Comment extends Model
{
    public function author()
    {
        return $this->belongsTo(User::class);
    }

    public function commentable()
    {
        // polymorphic: no related-model argument at all, so there is no
        // single real target to draw an edge to (same as Rails'
        // `polymorphic: true` / Django's GenericForeignKey)
        return $this->morphTo();
    }
}
