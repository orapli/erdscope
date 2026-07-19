<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Post extends Model
{
    public function user()
    {
        return $this->belongsTo(User::class);
    }

    public function parent()
    {
        return $this->belongsTo(Post::class, 'parent_id');
    }

    public function replies()
    {
        return $this->hasMany(Post::class, 'parent_id');
    }

    public function tags()
    {
        return $this->belongsToMany(Tag::class);
    }
}
